"""Environment volumes on the vm tier (ADR 0076): a setup build's EC2 instance gets an empty,
encrypted disk for each volume, which is kept when the instance ends and snapshotted; once
each snapshot is complete, and fast to restore in the template's zones when it asks, the
build has succeeded. Machines for the environment's runs then get a new disk from the
snapshot, mounted read-only, and each job records the version. EC2 here is
:class:`test_vm_tier.FakeEc2`; :mod:`test_ec2` checks the requests against EC2's model."""

import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.db.models import F
from django.utils import timezone
from org_world import World, build_world
from test_api_entities import api_as
from test_vm_tier import (
    WORKER_IMAGE,
    FakeEc2,
    a100,
    fleet,
    later,
    queue_run,
    register,
    tag,
)
from test_workers import WorkerClient

from iki_server.accounts.models import User
from iki_server.org.models import OrgRole, OrgRoleAssignment
from iki_server.runs import catalog, provisioner
from iki_server.runs.ec2 import LAUNCH_TAG, REMOVAL_TAG, SETUP_TAG, VOLUME_TAG
from iki_server.runs.ec2_fleet import Ec2Fleet
from iki_server.runs.models import (
    Job,
    Launch,
    LaunchState,
    ManagedVolume,
    SetupBuild,
    SetupState,
    VolumeState,
)

pytestmark = pytest.mark.django_db

ALPHAFOLD = "structure_prediction.alphafold-gpu"
GI = 1024**3
TI = 1024 * GI
ZONES = ["us-east-1a", "us-east-1b"]


def with_disks(**volume: Any) -> dict[str, Any]:
    return a100(
        volumes={
            "af-db": {
                "strategy": "clone_from_snapshot",
                "fast_snapshot_restore": True,
                "storage_class": None,
                "snapshot_class": None,
                "iops": 16000,
                "throughput": 1000,
                **volume,
            }
        }
    )


def use(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, template: dict[str, Any]) -> None:
    compute = json.loads(Path(catalog._compute_file()).read_text())  # type: ignore[arg-type]
    compute["templates"][template["name"]] = template
    compute["bindings"][ALPHAFOLD] = {
        "environment": ALPHAFOLD,
        "templates": [[template["name"], 0]],
    }
    file = tmp_path / "with-vm-volumes.json"
    file.write_text(json.dumps(compute))
    monkeypatch.setenv("IKI_COMPUTE_FILE", str(file))
    catalog.compute.cache_clear()


@pytest.fixture
def world(settings: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[World]:
    settings.IKI_RUNS_NAMESPACE = ""
    settings.IKI_RUNS_WORKER_IMAGE = WORKER_IMAGE
    settings.IKI_RUNS_VM_OWNER = "iki-test"
    settings.IKI_RUNS_VM_SERVER_URL = "https://iki.internal.example.com"
    settings.IKI_EGRESS_PROXY_URL = "http://iki-egress.iki.svc:3128"
    settings.IKI_RUNS_VM_EGRESS_PROXY_URL = "http://egress.internal.example.com:3128"
    use(monkeypatch, tmp_path, with_disks())
    made = build_world()
    other = User.objects.create_user("approver@example.com", "Ada")
    OrgRoleAssignment.objects.create(user=other, role=OrgRole.COMPUTE_ADMIN)
    made.users["approver"] = other
    yield made
    catalog.compute.cache_clear()


@pytest.fixture(autouse=True)
def keep_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    from iki_server.realtime import views

    monkeypatch.setattr(views, "close_old_connections", lambda: None)


def ask(world: World) -> dict[str, Any]:
    made = api_as(world, "compute_admin").post(
        "/compute/setups", {"environment": ALPHAFOLD, "reason": "AlphaFold's databases"}
    )
    assert made.status_code == 201, made.content
    (build,) = made.json()
    approved = api_as(world, "approver").post(f"/compute/setups/{build['hrid']}/approve")
    assert approved.status_code == 200, approved.content
    result: dict[str, Any] = build
    return result


def live(ec2: FakeEc2, launch: Launch) -> dict[str, Any]:
    (instance,) = [i for i in ec2.made.values() if tag(i, LAUNCH_TAG) == str(launch.pk)]
    return instance


def setup_launch() -> Launch:
    (launch,) = Launch.objects.exclude(setup=None).exclude(state=LaunchState.ENDED)
    return launch


def run_script(ec2: FakeEc2, machines: Ec2Fleet, data_version: str = "2026-10") -> None:
    """Its machine starts, its worker leases the build once its disks are known, the script
    succeeds, and the machine shuts itself down."""
    launch = setup_launch()
    instance = live(ec2, launch)
    instance["State"] = {"Name": "running"}
    worker = WorkerClient(register(ec2.token(instance)).json()["token"])
    provisioner.tick(machines)  # its disks are recorded now
    given = worker.lease().json()
    done = worker.post(
        f"/setups/{given['setup']['id']}/finish",
        {
            "lease_token": given["job"]["lease_token"],
            "outcome": "succeeded",
            "data_version": data_version,
        },
    )
    assert done.json() == {"state": "snapshotting"}, done.content
    ec2.end(instance["InstanceId"], "Client.InstanceInitiatedShutdown")


def build_version(world: World, ec2: FakeEc2, machines: Ec2Fleet) -> SetupBuild:
    """A build, through to its snapshot being ready for runs."""
    build = ask(world)
    provisioner.tick(machines)
    run_script(ec2, machines)
    provisioner.tick(machines)  # the launch ends; the snapshot is taken
    for snapshot in ec2.snaps.values():
        snapshot.update(State="completed", Progress="100%")
    provisioner.tick(machines)  # it's made fast to restore
    for zones in ec2.fast.values():
        for zone in zones:
            zones[zone] = "enabled"
    provisioner.tick(machines)
    done = SetupBuild.objects.get(hrid=build["hrid"])
    assert done.state == SetupState.SUCCEEDED, done.error
    return done


# --- A setup build's machine and disks -------------------------------------------------------


def test_a_setup_build_gets_an_on_demand_machine_with_an_empty_disk_it_keeps(
    world: World,
) -> None:
    build = ask(world)
    ec2 = FakeEc2()
    machines = fleet(ec2)
    assert provisioner.tick(machines) == (0, 0)  # a setup's machine isn't a run's
    launch = setup_launch()
    (request,) = ec2.requests
    assert "InstanceMarketOptions" not in request  # on demand, though the template says spot
    root, disk = request["BlockDeviceMappings"]
    assert root["DeviceName"] == "/dev/xvda"
    assert disk == {
        "DeviceName": "/dev/sdf",
        "Ebs": {
            "VolumeSize": 3 * 1024,
            "VolumeType": "gp3",
            "Encrypted": True,
            "DeleteOnTermination": False,
            "Iops": 16000,
            "Throughput": 1000,
            "KmsKeyId": "alias/iki-runs",
        },
    }
    (disk_tags,) = [t for t in request["TagSpecifications"] if t["ResourceType"] == "volume"]
    assert {"Key": SETUP_TAG, "Value": build["id"]} in disk_tags["Tags"]
    host = ec2.host(live(ec2, launch))
    assert host["volumes"] == [
        {
            "name": "af-db",
            "device": "/dev/sdf",
            "path": "/var/lib/iki/volumes/af-db",
            "mount": "/data/af",
            "writable": True,
        }
    ]
    assert host["once"] is True


def test_its_worker_leases_the_build_once_its_disks_are_known(world: World) -> None:
    build = ask(world)
    ec2 = FakeEc2()
    machines = fleet(ec2)
    provisioner.tick(machines)
    launch = setup_launch()
    instance = live(ec2, launch)
    instance["State"] = {"Name": "running"}
    worker = WorkerClient(register(ec2.token(instance)).json()["token"])
    # The provisioner hasn't seen which disk is af-db's yet: it couldn't snapshot it.
    assert worker.lease().status_code == 204
    provisioner.tick(machines)
    launch.refresh_from_db()
    disk = instance["BlockDeviceMappings"][1]["Ebs"]["VolumeId"]
    assert launch.volumes == {"af-db": {"disk": disk}}
    assert tag(ec2.disks[disk], VOLUME_TAG) == "af-db"
    given = worker.lease().json()
    assert given["kind"] == "SetupInput"
    assert given["setup"]["hrid"] == build["hrid"]
    assert given["volumes"]["af-db"]["mount"] == "/data/af"
    # From its VPC, through the vm tier's way to the egress proxy.
    assert given["job"]["proxy"].endswith("@egress.internal.example.com:3128")


def test_its_disk_is_snapshotted_once_detached_then_made_fast_to_restore(world: World) -> None:
    build = ask(world)
    ec2 = FakeEc2()
    machines = fleet(ec2)
    provisioner.tick(machines)
    run_script(ec2, machines)
    (disk,) = setup_launch().volumes.values()
    provisioner.tick(machines)
    (snapshot,) = ec2.snaps.values()
    assert snapshot["VolumeId"] == disk["disk"]
    assert tag(snapshot, SETUP_TAG) == build["id"]
    assert tag(snapshot, VOLUME_TAG) == "af-db"
    assert snapshot["Description"] == (f"{ALPHAFOLD} af-db, version 1 ({build['hrid']})")
    shown = api_as(world, "compute_admin").get(f"/compute/setups/{build['id']}").json()
    assert shown["state"] == "snapshotting"
    snapshot["Progress"] = "42%"
    provisioner.tick(machines)
    shown = api_as(world, "compute_admin").get(f"/compute/setups/{build['id']}").json()
    assert shown["volumes"][0]["handle"] == snapshot["SnapshotId"]
    assert shown["volumes"][0]["message"] == "42% copied"
    snapshot.update(State="completed", Progress="100%")
    provisioner.tick(machines)
    assert ec2.fast[snapshot["SnapshotId"]] == dict.fromkeys(ZONES, "enabling")
    ec2.fast[snapshot["SnapshotId"]][ZONES[0]] = "optimizing"
    provisioner.tick(machines)
    record = ManagedVolume.objects.get()
    assert record.state == VolumeState.CREATING
    assert record.message == (
        "Being made fast to restore: us-east-1a (optimizing), us-east-1b (enabling)"
    )
    assert SetupBuild.objects.get(pk=build["id"]).state == SetupState.SNAPSHOTTING
    ec2.fast[snapshot["SnapshotId"]] = dict.fromkeys(ZONES, "enabled")
    provisioner.tick(machines)
    done = SetupBuild.objects.get(pk=build["id"])
    assert (done.state, done.data_version) == (SetupState.SUCCEEDED, "2026-10")
    record.refresh_from_db()
    assert (record.state, record.message) == (VolumeState.READY, "")
    # Its disk has served its purpose.
    assert ec2.disks[disk["disk"]]["State"] == "deleted"


def test_without_fast_restore_a_completed_snapshot_is_ready(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use(monkeypatch, tmp_path, with_disks(fast_snapshot_restore=False))
    build = ask(world)
    ec2 = FakeEc2()
    machines = fleet(ec2)
    provisioner.tick(machines)
    run_script(ec2, machines)
    provisioner.tick(machines)
    for snapshot in ec2.snaps.values():
        snapshot.update(State="completed", Progress="100%")
    provisioner.tick(machines)
    assert SetupBuild.objects.get(pk=build["id"]).state == SetupState.SUCCEEDED
    assert not ec2.fast


def test_fast_restore_ec2_wont_give_yet_is_asked_for_again(world: World) -> None:
    build = ask(world)
    ec2 = FakeEc2()
    machines = fleet(ec2)
    provisioner.tick(machines)
    run_script(ec2, machines)
    provisioner.tick(machines)
    for snapshot in ec2.snaps.values():
        snapshot.update(State="completed", Progress="100%")
    ec2.no_fast = "FastSnapshotRestoreLimitExceeded: 5 snapshots at most"
    provisioner.tick(machines)
    record = ManagedVolume.objects.get()
    assert record.message.startswith(
        "EC2 wouldn't yet make it fast to restore: FastSnapshotRestoreLimitExceeded"
    )
    assert "only so many snapshots fast to restore in a region" in record.message
    assert SetupBuild.objects.get(pk=build["id"]).state == SetupState.SNAPSHOTTING
    ec2.no_fast = ""
    provisioner.tick(machines)
    assert set(next(iter(ec2.fast.values())).values()) == {"enabling"}


def test_a_snapshot_that_fails_fails_the_build_and_goes(world: World) -> None:
    build = ask(world)
    ec2 = FakeEc2()
    machines = fleet(ec2)
    provisioner.tick(machines)
    run_script(ec2, machines)
    provisioner.tick(machines)
    (snapshot,) = ec2.snaps.values()
    snapshot.update(State="error", StateMessage="KMS key disabled")
    provisioner.tick(machines)
    failed = SetupBuild.objects.get(pk=build["id"])
    assert failed.state == SetupState.FAILED
    assert failed.error == {
        "code": "snapshot_failed",
        "message": "Its snapshot of af-db failed: KMS key disabled",
    }
    provisioner.tick(machines, later(6))  # tidying, a while later
    assert snapshot["State"] == "deleted"
    assert {d["State"] for d in ec2.disks.values()} == {"deleted"}


def test_a_build_with_no_machine_to_be_had_waits_saying_so(world: World) -> None:
    build = ask(world)
    ec2 = FakeEc2()
    ec2.no["all"] = "InsufficientInstanceCapacity"
    machines = fleet(ec2)
    provisioner.tick(machines)
    shown = api_as(world, "compute_admin").get(f"/compute/setups/{build['id']}").json()
    assert shown["state"] == "queued"
    assert "EC2 has no p4d.24xlarge free" in shown["progress_message"]
    # It waits a while before trying again, and tries however often it must.
    provisioner.tick(machines)
    assert len(ec2.requests) == 2  # on demand, in each zone
    for minutes in range(1, 6):
        Launch.objects.update(ended_at=F("ended_at") - timedelta(minutes=11))
        provisioner.tick(machines, later(minutes))
    assert SetupBuild.objects.get(pk=build["id"]).state == SetupState.QUEUED
    assert len(ec2.requests) == 12
    ec2.no.clear()
    Launch.objects.update(ended_at=F("ended_at") - timedelta(minutes=11))
    provisioner.tick(machines)
    assert setup_launch().machine


def test_a_setup_machine_ec2_refuses_fails_the_build_not_the_runs(world: World) -> None:
    build = ask(world)
    run = queue_run(world)
    ec2 = FakeEc2()
    ec2.no["all"] = "UnauthorizedOperation"
    machines = fleet(ec2)
    for attempt in range(3):
        provisioner.tick(machines, later(attempt))
        Launch.objects.update(ended_at=F("ended_at") - timedelta(minutes=11))
    provisioner.tick(machines, later(4))
    failed = SetupBuild.objects.get(pk=build["id"])
    assert failed.state == SetupState.FAILED
    assert failed.error is not None
    assert "UnauthorizedOperation" in failed.error["message"]
    shown = api_as(world, "runner").get(f"/runs/{run['hrid']}").json()
    assert shown["status"] == "queued"


# --- Machines for runs ----------------------------------------------------------------------


def test_runs_machines_get_a_read_only_copy_of_the_newest_version(world: World) -> None:
    ec2 = FakeEc2()
    machines = fleet(ec2)
    version = build_version(world, ec2, machines)
    record = ManagedVolume.objects.get(build=version)
    run = queue_run(world)
    assert provisioner.tick(machines) == (1, 0)
    request = ec2.requests[-1]
    assert request["InstanceMarketOptions"]["MarketType"] == "spot"
    _, disk = request["BlockDeviceMappings"]
    assert disk == {
        "DeviceName": "/dev/sdf",
        "Ebs": {
            "VolumeSize": 3 * 1024,
            "VolumeType": "gp3",
            "Encrypted": True,
            "DeleteOnTermination": True,
            "SnapshotId": record.handle,
            "Iops": 16000,
            "Throughput": 1000,
            "KmsKeyId": "alias/iki-runs",
        },
    }
    launch = Launch.objects.get(setup=None)
    instance = live(ec2, launch)
    assert ec2.host(instance)["volumes"] == [
        {
            "name": "af-db",
            "device": "/dev/sdf",
            "path": "/var/lib/iki/volumes/af-db",
            "mount": "/data/af",
            "writable": False,
        }
    ]
    provenance = {
        "af-db": {
            "build": str(version.pk),
            "hrid": version.hrid,
            "version": 1,
            "data_version": "2026-10",
        }
    }
    assert launch.volumes == provenance
    worker = WorkerClient(register(ec2.token(instance)).json()["token"])
    given = worker.lease().json()
    assert given["volumes"] == provenance
    assert Job.objects.get(run_id=run["id"]).volumes == provenance


def test_a_retired_version_stops_being_fast_and_goes_once_no_machine_has_it(
    world: World,
) -> None:
    ec2 = FakeEc2()
    machines = fleet(ec2)
    first = build_version(world, ec2, machines)
    (old,) = ManagedVolume.objects.filter(build=first).values_list("handle", flat=True)
    queue_run(world)
    provisioner.tick(machines)
    holder = Launch.objects.get(setup=None, state=LaunchState.STARTING)
    retired = api_as(world, "compute_admin").post(f"/compute/setups/{first.hrid}/retire")
    assert retired.status_code == 200, retired.content
    provisioner.tick(machines, later(6))
    # New machines don't get it, so it needn't start at full speed any more.
    assert ec2.fast[old] == dict.fromkeys(ZONES, "disabling")
    assert ec2.snaps[old]["State"] == "completed"  # a machine has a copy
    ec2.end(live(ec2, holder)["InstanceId"], "Client.UserInitiatedShutdown")
    provisioner.tick(machines, later(12))
    provisioner.tick(machines, later(18))
    assert ec2.snaps[old]["State"] == "deleted"
    assert ManagedVolume.objects.get(handle=old).state == VolumeState.DELETED


def test_only_the_version_runs_get_stays_fast_to_restore(world: World) -> None:
    ec2 = FakeEc2()
    machines = fleet(ec2)
    first = build_version(world, ec2, machines)
    (old,) = ManagedVolume.objects.filter(build=first).values_list("handle", flat=True)
    second = build_version(world, ec2, machines)
    (new,) = ManagedVolume.objects.filter(build=second).values_list("handle", flat=True)
    assert set(ec2.fast[old].values()) == {"enabled"}  # until the next tidy
    provisioner.tick(machines, later(6))
    # Superseded: kept to roll back to, but no longer costing by the hour to be fast.
    assert ec2.fast[old] == dict.fromkeys(ZONES, "disabling")
    assert set(ec2.fast[new].values()) == {"enabled"}
    assert ec2.snaps[old]["State"] == "completed"
    ec2.fast[old] = dict.fromkeys(ZONES, "disabled")
    # Retiring the second makes the first the version runs get: fast again.
    retired = api_as(world, "compute_admin").post(f"/compute/setups/{second.hrid}/retire")
    assert retired.status_code == 200, retired.content
    provisioner.tick(machines, later(12))
    assert ec2.snaps[new]["State"] == "deleted"
    assert ec2.fast[old] == dict.fromkeys(ZONES, "enabling")
    record = ManagedVolume.objects.get(handle=old)
    assert record.state == VolumeState.READY  # runs get it meanwhile, a little slower
    assert record.message.startswith("Being made fast to restore: us-east-1a (enabling)")
    ec2.fast[old] = dict.fromkeys(ZONES, "enabled")
    provisioner.tick(machines, later(18))
    record.refresh_from_db()
    assert record.message == ""


def test_disks_and_snapshots_with_no_build_are_deleted(world: World) -> None:
    ec2 = FakeEc2()
    machines = fleet(ec2)
    ec2.disks["vol-0stray"] = {
        "VolumeId": "vol-0stray",
        "State": "available",
        "Tags": [
            {"Key": "iki.ikisci.org/deployment", "Value": "iki-test"},
            {"Key": SETUP_TAG, "Value": "0193a1b2-0000-7000-8000-000000000000"},
        ],
        "ebs": {"DeleteOnTermination": False},
        "region": "us-east-1",
    }
    ec2.snaps["snap-0stray"] = {
        "SnapshotId": "snap-0stray",
        "State": "completed",
        "Tags": [
            {"Key": "iki.ikisci.org/deployment", "Value": "iki-test"},
            {"Key": SETUP_TAG, "Value": "not-a-build"},
        ],
        "region": "us-east-1",
    }
    provisioner.tick(machines, timezone.now())
    assert ec2.disks["vol-0stray"]["State"] == "deleted"
    assert ec2.snaps["snap-0stray"]["State"] == "deleted"


# --- On a shared file system (ADR 0077) ----------------------------------------------------------

LUSTRE = "fs-0123.fsx.us-east-1.amazonaws.com@tcp:/abcdefgh"


def on_lustre(**aws: Any) -> dict[str, Any]:
    template = a100(
        volumes={"af-db": {"strategy": "shared_filesystem", "lustre": LUSTRE, "path": "reference"}}
    )
    template["aws"] = {**template["aws"], **aws}
    return template


def shared_version(world: World, ec2: FakeEc2, machines: Ec2Fleet) -> SetupBuild:
    """A build on the shared file system, through to its version being ready."""
    build = ask(world)
    provisioner.tick(machines)
    run_script(ec2, machines)
    provisioner.tick(machines)
    done = SetupBuild.objects.get(hrid=build["hrid"])
    assert done.state == SetupState.SUCCEEDED, done.error
    return done


def cleaners(ec2: FakeEc2) -> list[dict[str, Any]]:
    return [i for i in ec2.made.values() if tag(i, REMOVAL_TAG)]


def test_on_a_shared_file_system_a_setup_fills_its_versions_own_directory(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use(monkeypatch, tmp_path, on_lustre())
    ec2 = FakeEc2()
    machines = fleet(ec2)
    build = ask(world)
    provisioner.tick(machines)
    launch = setup_launch()
    instance = live(ec2, launch)
    # No disk of its own: the machine mounts the file system.
    (root,) = instance["request"]["BlockDeviceMappings"]
    assert root["DeviceName"] == "/dev/xvda"
    directory = f"{ALPHAFOLD}/af-db/{build['hrid']}"
    assert ec2.host(instance)["volumes"] == [
        {
            "name": "af-db",
            "filesystem": {"type": "lustre", "source": LUSTRE},
            "root": "reference",
            "directory": directory,
            "path": "/var/lib/iki/volumes/af-db",
            "mount": "/data/af",
            "writable": True,
        }
    ]
    run_script(ec2, machines)
    assert Launch.objects.get(pk=launch.pk).volumes == {
        "af-db": {"filesystem": {"type": "lustre", "source": LUSTRE}, "directory": directory}
    }
    provisioner.tick(machines)
    done = SetupBuild.objects.get(pk=build["id"])
    assert done.state == SetupState.SUCCEEDED, done.error
    volume = ManagedVolume.objects.get(build=done)
    assert (volume.state, volume.handle) == (VolumeState.READY, directory)
    assert not ec2.snaps
    # Runs' machines mount it, and get the version's directory read-only.
    queue_run(world)
    provisioner.tick(machines)
    (run,) = Launch.objects.filter(setup=None)
    (given,) = ec2.host(live(ec2, run))["volumes"]
    assert (given["directory"], given["writable"]) == (directory, False)
    assert run.volumes["af-db"]["hrid"] == done.hrid


def test_a_retired_version_is_removed_by_a_small_machine_once_unused(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use(monkeypatch, tmp_path, on_lustre())
    ec2 = FakeEc2()
    machines = fleet(ec2)
    version = shared_version(world, ec2, machines)
    queue_run(world)
    provisioner.tick(machines)
    holder = Launch.objects.get(setup=None)
    api_as(world, "compute_admin").post(f"/compute/setups/{version.hrid}/retire")
    provisioner.tick(machines, later(6))
    assert not cleaners(ec2)  # a machine has it mounted
    ec2.end(live(ec2, holder)["InstanceId"], "Client.UserInitiatedShutdown")
    provisioner.tick(machines, later(12))
    (cleaner,) = cleaners(ec2)
    request = cleaner["request"]
    # Small, on demand, in the template's network, with its image and role; no launch.
    assert request["InstanceType"] == "t3.small"
    assert "InstanceMarketOptions" not in request
    assert request["ImageId"] == on_lustre()["aws"]["image"]
    assert request["NetworkInterfaces"][0]["AssociatePublicIpAddress"] is False
    assert request["MetadataOptions"]["HttpTokens"] == "required"
    (root,) = request["BlockDeviceMappings"]
    assert root["Ebs"]["Encrypted"] is True
    assert tag(cleaner, LAUNCH_TAG) is None
    assert tag(cleaner, SETUP_TAG) == str(version.pk)
    assert ec2.host(cleaner) == {
        "name": f"iki-cleanup-{version.hrid.lower()}",
        "user": "10001:10001",
        "worker": "/opt/iki/bin/iki-worker",
        "remove": [
            {
                "name": "af-db",
                "filesystem": {"type": "lustre", "source": LUSTRE},
                "root": "reference",
                "directory": f"{ALPHAFOLD}/af-db/{version.hrid}",
                "path": "/var/lib/iki/volumes/af-db",
            }
        ],
    }
    # The fleet leaves it be: it isn't a stray run machine.
    cleaner["State"] = {"Name": "running"}
    provisioner.tick(machines, later(13))
    assert cleaner["InstanceId"] not in ec2.terminated
    # Once it says it removed the version, the version's deleted.
    ec2.consoles[cleaner["InstanceId"]] = "...\nIKI-REMOVED\n"
    ec2.end(cleaner["InstanceId"], "Client.InstanceInitiatedShutdown")
    provisioner.tick(machines, later(18))
    gone = ManagedVolume.objects.get(build=version)
    assert (gone.state, gone.deleted_at is not None) == (VolumeState.DELETED, True)
    provisioner.tick(machines, later(24))
    assert len(cleaners(ec2)) == 1


def test_a_failed_builds_directory_is_removed_and_a_failed_removal_tried_again(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use(monkeypatch, tmp_path, on_lustre(cleanup_instance_type="t4g.small"))
    ec2 = FakeEc2()
    machines = fleet(ec2)
    build = ask(world)
    provisioner.tick(machines)
    launch = setup_launch()
    instance = live(ec2, launch)
    instance["State"] = {"Name": "running"}
    worker = WorkerClient(register(ec2.token(instance)).json()["token"])
    provisioner.tick(machines)
    given = worker.lease().json()
    worker.post(
        f"/setups/{given['setup']['id']}/finish",
        {"lease_token": given["job"]["lease_token"], "outcome": "failed", "message": "exit 1"},
    )
    # Not while its machine may still be writing.
    provisioner.tick(machines, later(6))
    assert not cleaners(ec2)
    ec2.end(instance["InstanceId"], "Client.InstanceInitiatedShutdown")
    provisioner.tick(machines, later(12))
    (first,) = cleaners(ec2)
    first["LaunchTime"] = later(12)  # when that round started it
    assert first["request"]["InstanceType"] == "t4g.small"
    ec2.consoles[first["InstanceId"]] = (
        "IKI-HOST-ERROR removal_failed: af-db: mount.lustre: mount failed\n"
    )
    ec2.end(first["InstanceId"], "Client.InstanceInitiatedShutdown")
    provisioner.tick(machines, later(18))
    record = ManagedVolume.objects.get(build_id=build["id"])
    assert "mount.lustre: mount failed" in record.message
    assert len(cleaners(ec2)) == 1  # not again at once
    provisioner.tick(machines, later(30))
    assert len(cleaners(ec2)) == 2
    second = cleaners(ec2)[-1]
    second["LaunchTime"] = later(30)
    ec2.consoles[second["InstanceId"]] = "IKI-REMOVED\n"
    ec2.end(second["InstanceId"], "Client.InstanceInitiatedShutdown")
    provisioner.tick(machines, later(36))
    gone = ManagedVolume.objects.get(build_id=build["id"])
    assert (gone.state, gone.deleted_at is not None, gone.message) == (VolumeState.FAILED, True, "")


# --- In an S3 bucket (ADR 0078) -----------------------------------------------------------------

BUCKET = "acme-reference"


def in_a_bucket() -> dict[str, Any]:
    return a100(
        volumes={"af-db": {"strategy": "object_mount", "bucket": BUCKET, "path": "reference"}}
    )


def copy_into_bucket(
    world: World, ec2: FakeEc2, machines: Ec2Fleet
) -> tuple[uuid.UUID, dict[str, Any]]:
    """A build whose script succeeds and whose worker then goes, leaving its machine to copy
    its disk into the bucket. Returns the build's ID and its machine."""
    build = ask(world)
    provisioner.tick(machines)
    launch = setup_launch()
    instance = live(ec2, launch)
    instance["State"] = {"Name": "running"}
    worker = WorkerClient(register(ec2.token(instance)).json()["token"])
    provisioner.tick(machines)  # its disk is recorded now
    given = worker.lease().json()
    done = worker.post(
        f"/setups/{given['setup']['id']}/finish",
        {"lease_token": given["job"]["lease_token"], "outcome": "succeeded"},
    )
    assert done.json() == {"state": "snapshotting"}, done.content
    assert worker.post("/deregister").status_code == 204
    return uuid.UUID(build["id"]), instance


def test_in_a_bucket_a_setup_fills_a_disk_its_machine_then_copies_there(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use(monkeypatch, tmp_path, in_a_bucket())
    ec2 = FakeEc2()
    machines = fleet(ec2)
    build_id, instance = copy_into_bucket(world, ec2, machines)
    hrid = SetupBuild.objects.get(pk=build_id).hrid
    directory = f"{ALPHAFOLD}/af-db/{hrid}"
    # An empty disk of its own, kept when it ends, which the machine copies into the
    # version's directory in the bucket once the worker says the build succeeded.
    _, data = instance["request"]["BlockDeviceMappings"]
    assert data["DeviceName"] == "/dev/sdf"
    assert data["Ebs"]["DeleteOnTermination"] is False
    host = ec2.host(instance)
    assert host["volumes"] == [
        {
            "name": "af-db",
            "path": "/var/lib/iki/volumes/af-db",
            "mount": "/data/af",
            "writable": True,
            "device": "/dev/sdf",
            "publish": {
                "name": "af-db",
                "filesystem": {"type": "s3", "source": BUCKET},
                "root": "reference",
                "directory": directory,
                "path": "/var/lib/iki/buckets/af-db",
            },
        }
    ]
    assert host["succeeded_file"] == "/var/lib/iki/scratch/.iki-setup-succeeded"
    assert host["env"]["IKI_SETUP_SUCCEEDED_FILE"] == "/iki/scratch/.iki-setup-succeeded"
    (disk,) = [d for d, v in ec2.disks.items() if v["ebs"].get("DeleteOnTermination") is False]
    assert Launch.objects.get(setup_id=build_id).volumes == {"af-db": {"disk": disk}}
    assert ManagedVolume.objects.get(build_id=build_id).handle == directory
    # Its worker has gone, but the fleet leaves its machine to finish copying.
    provisioner.tick(machines, later(1))
    assert instance["InstanceId"] not in ec2.terminated
    assert SetupBuild.objects.get(pk=build_id).state == SetupState.SNAPSHOTTING
    # Once it's ended, saying it copied the disk, the version's ready and the disk goes.
    ec2.consoles[instance["InstanceId"]] = "...\r\nIKI-PUBLISHED af-db\r\n"
    ec2.end(instance["InstanceId"], "Client.InstanceInitiatedShutdown")
    provisioner.tick(machines, later(2))
    done = SetupBuild.objects.get(pk=build_id)
    assert done.state == SetupState.SUCCEEDED, done.error
    volume = ManagedVolume.objects.get(build=done)
    assert (volume.state, volume.handle) == (VolumeState.READY, directory)
    assert ec2.disks[disk]["State"] == "deleted"
    assert not ec2.snaps
    # Runs' machines mount the bucket, and get the version's directory read-only.
    queue_run(world)
    provisioner.tick(machines, later(3))
    (run,) = Launch.objects.filter(setup=None)
    run_machine = live(ec2, run)
    (only_root,) = run_machine["request"]["BlockDeviceMappings"]
    assert only_root["DeviceName"] == "/dev/xvda"
    (given,) = ec2.host(run_machine)["volumes"]
    assert given["filesystem"] == {"type": "s3", "source": BUCKET}
    assert (given["directory"], given["writable"]) == (directory, False)
    assert "succeeded_file" not in ec2.host(run_machine)


def test_a_copy_into_a_bucket_that_failed_fails_the_build_and_is_removed(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use(monkeypatch, tmp_path, in_a_bucket())
    ec2 = FakeEc2()
    machines = fleet(ec2)
    build_id, instance = copy_into_bucket(world, ec2, machines)
    ec2.consoles[instance["InstanceId"]] = (
        "IKI-HOST-ERROR publish_failed: af-db (x): Access Denied\r\n"
    )
    ec2.end(instance["InstanceId"], "Client.InstanceInitiatedShutdown")
    provisioner.tick(machines, later(1))
    build = SetupBuild.objects.get(pk=build_id)
    assert build.state == SetupState.FAILED
    assert build.error is not None
    assert build.error["message"] == (
        "Iki couldn't copy af-db into its bucket: af-db (x): Access Denied"
    )
    # Whatever it copied is removed by a small machine, mounting the bucket.
    provisioner.tick(machines, later(7))
    (cleaner,) = cleaners(ec2)
    (removal,) = ec2.host(cleaner)["remove"]
    assert removal["filesystem"] == {"type": "s3", "source": BUCKET}
    assert removal["directory"] == f"{ALPHAFOLD}/af-db/{build.hrid}"
    # Its disk is deleted.
    (disk,) = [d for d in ec2.disks.values() if d["ebs"].get("DeleteOnTermination") is False]
    assert disk["State"] == "deleted"


def test_a_machine_that_copies_too_long_or_never_says_fails_the_build(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use(monkeypatch, tmp_path, in_a_bucket())
    ec2 = FakeEc2()
    machines = fleet(ec2)
    build_id, instance = copy_into_bucket(world, ec2, machines)
    provisioner.tick(machines, later(60 * 23))
    assert SetupBuild.objects.get(pk=build_id).state == SetupState.SNAPSHOTTING
    provisioner.tick(machines, later(60 * 25))
    build = SetupBuild.objects.get(pk=build_id)
    assert build.state == SetupState.FAILED
    assert build.error is not None
    assert "took too long to copy af-db" in build.error["message"]
    # No longer copying for a build, it's a stray: terminated.
    provisioner.tick(machines, later(60 * 25 + 1))
    assert instance["InstanceId"] in ec2.terminated
    # One that ended without saying, once EC2 has had time to post its console.
    second_id, second = copy_into_bucket(world, ec2, machines)
    ec2.end(second["InstanceId"], "Client.InstanceInitiatedShutdown")
    provisioner.tick(machines, later(10))
    assert SetupBuild.objects.get(pk=second_id).state == SetupState.SNAPSHOTTING
    provisioner.tick(machines, later(45))
    failed = SetupBuild.objects.get(pk=second_id)
    assert failed.state == SetupState.FAILED
    assert failed.error is not None
    assert failed.error["message"] == "Its machine didn't say it copied af-db into its bucket."
