"""Environment volumes on Compute Engine (ADR 0076, 0107): a setup build's instance gets an
empty, encrypted persistent disk for each volume, kept when the instance is deleted and
made into an image; once each image is ready, the build has succeeded. Machines for the
environment's runs then get a new disk from the image, mounted read-only and deleted with
them, and each job records the version. Compute Engine here is
:class:`test_vm_tier_gce.FakeGce`."""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.db.models import F
from django.utils import timezone
from org_world import World, build_world
from test_api_entities import api_as
from test_vm_tier import WORKER_IMAGE, later, queue_run, register
from test_vm_tier_gce import KEY, PROJECT, SOURCE, FakeGce, a100
from test_vm_volumes import ALPHAFOLD, ask, use
from test_workers import WorkerClient

from iki_server.accounts.models import User
from iki_server.org.models import OrgRole, OrgRoleAssignment
from iki_server.runs import catalog, provisioner
from iki_server.runs.gce import LAUNCH_LABEL, OWNER_LABEL, SETUP_LABEL, VOLUME_LABEL
from iki_server.runs.gce_fleet import GceFleet
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

GI = 1024**3


def with_disks(**volume: Any) -> dict[str, Any]:
    return a100(
        volumes={
            "af-db": {
                "strategy": "clone_from_snapshot",
                "fast_snapshot_restore": False,
                "storage_class": None,
                "snapshot_class": None,
                **volume,
            }
        }
    )


@pytest.fixture
def world(settings: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[World]:
    settings.IKI_RUNS_NAMESPACE = ""
    settings.IKI_RUNS_WORKER_IMAGE = WORKER_IMAGE
    settings.IKI_RUNS_VM_OWNER = "iki-test"
    settings.IKI_RUNS_VM_CLOUD = "gcp"
    settings.IKI_RUNS_VM_GCP_PROJECT = PROJECT
    settings.IKI_RUNS_VM_GCP_INSTANCE_TEMPLATE = SOURCE
    settings.IKI_RUNS_VM_GCP_KMS_KEY = KEY
    settings.IKI_RUNS_VM_SERVER_URL = "https://iki.internal.example.com"
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


def fleet(gce: FakeGce) -> GceFleet:
    return GceFleet(gce)


def live(gce: FakeGce, launch: Launch) -> dict[str, Any]:
    (instance,) = [
        i
        for i in gce.made.values()
        if i["labels"].get(LAUNCH_LABEL) == str(launch.pk) and i["status"] != "DELETED"
    ]
    return instance


def setup_launch() -> Launch:
    (launch,) = Launch.objects.exclude(setup=None).exclude(state=LaunchState.ENDED)
    return launch


def run_script(gce: FakeGce, machines: GceFleet, data_version: str = "2026-10") -> None:
    """Its machine starts, its worker leases the build once its disks are known, the script
    succeeds, and the machine shuts itself down."""
    instance = live(gce, setup_launch())
    instance["status"] = "RUNNING"
    worker = WorkerClient(register(gce.token(instance)).json()["token"])
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
    instance["status"] = "TERMINATED"


def build_version(world: World, gce: FakeGce, machines: GceFleet) -> SetupBuild:
    """A build, through to its image being ready for runs."""
    build = ask(world)
    provisioner.tick(machines)
    run_script(gce, machines)
    provisioner.tick(machines)  # its machine is deleted; the image is asked for
    provisioner.tick(machines)
    for image in gce.images_made.values():
        image["status"] = "READY"
    provisioner.tick(machines)
    done = SetupBuild.objects.get(hrid=build["hrid"])
    assert done.state == SetupState.SUCCEEDED, done.error
    return done


# --- A setup build's machine and disks -------------------------------------------------------


def test_a_setup_build_gets_a_standard_machine_with_an_empty_disk_it_keeps(
    world: World,
) -> None:
    build = ask(world)
    gce = FakeGce()
    machines = fleet(gce)
    assert provisioner.tick(machines) == (0, 0)  # a setup's machine isn't a run's
    launch = setup_launch()
    (request,) = gce.requests
    instance = request["instance"]
    # Standard, though the template says Spot: one taken back would waste the download.
    assert instance["scheduling"]["provisioningModel"] == "STANDARD"
    assert instance["labels"][SETUP_LABEL] == build["id"]
    boot, disk = instance["disks"]
    assert boot["boot"] is True
    zone = request["zone"]
    assert disk == {
        "deviceName": "iki-volume-1",
        "autoDelete": False,
        "initializeParams": {
            "diskName": f"{launch.machine}-d1",
            "diskSizeGb": str(3 * 1024),
            "diskType": f"zones/{zone}/diskTypes/pd-balanced",
            "labels": {**instance["labels"], VOLUME_LABEL: "af-db"},
        },
        "diskEncryptionKey": {"kmsKeyName": KEY},
    }
    assert disk["initializeParams"]["diskName"].startswith("iki-test-")
    host = gce.host(live(gce, launch))
    assert host["volumes"] == [
        {
            "name": "af-db",
            "device": "/dev/disk/by-id/google-iki-volume-1",
            "path": "/var/lib/iki/volumes/af-db",
            "mount": "/data/af",
            "writable": True,
        }
    ]


def test_its_worker_leases_the_build_once_its_disks_are_known(world: World) -> None:
    build = ask(world)
    gce = FakeGce()
    machines = fleet(gce)
    provisioner.tick(machines)
    launch = setup_launch()
    instance = live(gce, launch)
    instance["status"] = "RUNNING"
    worker = WorkerClient(register(gce.token(instance)).json()["token"])
    gce.listing_disks = False  # Compute Engine doesn't list them attached yet
    assert worker.lease().status_code == 204
    provisioner.tick(machines)
    assert worker.lease().status_code == 204
    gce.listing_disks = True
    provisioner.tick(machines)
    launch.refresh_from_db()
    assert launch.volumes == {"af-db": {"disk": f"{launch.machine}-d1", "zone": launch.zone}}
    given = worker.lease().json()
    assert given["kind"] == "SetupInput"
    assert given["setup"]["hrid"] == build["hrid"]


def test_its_disk_is_made_into_an_image_once_detached(world: World) -> None:
    build = ask(world)
    gce = FakeGce()
    machines = fleet(gce)
    provisioner.tick(machines)
    run_script(gce, machines)
    launch = setup_launch()
    disk = launch.volumes["af-db"]
    provisioner.tick(machines)
    assert gce.made[launch.machine]["status"] == "DELETED"
    assert gce.disks_made[(disk["zone"], disk["disk"])]["users"] == []  # kept
    (image,) = gce.images_made.values()
    hrid = build["hrid"].lower()
    assert image["name"] == f"iki-test-{hrid}-v1"
    assert image["sourceDisk"] == f"projects/{PROJECT}/zones/{disk['zone']}/disks/{disk['disk']}"
    assert image["storageLocations"] == ["us-central1"]
    assert image["imageEncryptionKey"] == {"kmsKeyName": KEY}
    assert image["labels"] == {
        OWNER_LABEL: "iki-test",
        SETUP_LABEL: build["id"],
        VOLUME_LABEL: "af-db",
        "iki-environment": "acme-prediction-alphafold-gpu",
    }
    assert image["description"] == f"{ALPHAFOLD} af-db, version 1 ({build['hrid']})"
    provisioner.tick(machines)
    record = ManagedVolume.objects.get()
    assert record.handle == f"projects/{PROJECT}/global/images/iki-test-{hrid}-v1"
    assert (record.state, record.message) == (VolumeState.CREATING, "Being copied into an image")
    assert SetupBuild.objects.get(pk=build["id"]).state == SetupState.SNAPSHOTTING
    image["status"] = "READY"
    provisioner.tick(machines)
    done = SetupBuild.objects.get(pk=build["id"])
    assert (done.state, done.data_version) == (SetupState.SUCCEEDED, "2026-10")
    record.refresh_from_db()
    assert (record.state, record.message) == (VolumeState.READY, "")
    # Its disk has served its purpose.
    assert gce.disks_made[(disk["zone"], disk["disk"])]["status"] == "DELETED"


def test_an_image_that_fails_fails_the_build_and_goes(world: World) -> None:
    build = ask(world)
    gce = FakeGce()
    machines = fleet(gce)
    provisioner.tick(machines)
    run_script(gce, machines)
    provisioner.tick(machines)
    (image,) = gce.images_made.values()
    provisioner.tick(machines)
    image["status"] = "FAILED"
    provisioner.tick(machines)
    failed = SetupBuild.objects.get(pk=build["id"])
    assert failed.state == SetupState.FAILED
    assert failed.error == {
        "code": "snapshot_failed",
        "message": "Compute Engine couldn't make an image of af-db.",
    }
    provisioner.tick(machines, later(6))  # tidying, a while later
    assert image["status"] == "DELETED"
    assert {d["status"] for d in gce.disks_made.values()} == {"DELETED"}
    assert ManagedVolume.objects.get().state == VolumeState.FAILED


def test_a_build_with_no_machine_to_be_had_waits_saying_so(world: World) -> None:
    build = ask(world)
    gce = FakeGce()
    gce.no["all"] = "ZONE_RESOURCE_POOL_EXHAUSTED"
    machines = fleet(gce)
    provisioner.tick(machines)
    shown = api_as(world, "compute_admin").get(f"/compute/setups/{build['id']}").json()
    assert shown["state"] == "queued"
    assert "Compute Engine has no a2-highgpu-8g free" in shown["progress_message"]
    assert len(gce.requests) == 2  # standard, in each zone
    gce.no.clear()
    Launch.objects.update(ended_at=F("ended_at") - timedelta(minutes=11))
    provisioner.tick(machines)
    assert setup_launch().machine


def test_a_setup_machine_compute_engine_refuses_fails_the_build_not_the_runs(
    world: World,
) -> None:
    build = ask(world)
    run = queue_run(world)
    gce = FakeGce()
    gce.no["all"] = "forbidden"
    machines = fleet(gce)
    for attempt in range(3):
        provisioner.tick(machines, later(attempt))
        Launch.objects.update(ended_at=F("ended_at") - timedelta(minutes=11))
    provisioner.tick(machines, later(4))
    failed = SetupBuild.objects.get(pk=build["id"])
    assert failed.state == SetupState.FAILED
    assert failed.error is not None
    assert "forbidden" in failed.error["message"]
    shown = api_as(world, "runner").get(f"/runs/{run['hrid']}").json()
    assert shown["status"] == "queued"


# --- Machines for runs ----------------------------------------------------------------------


def test_runs_machines_get_a_read_only_copy_of_the_newest_version(world: World) -> None:
    gce = FakeGce()
    machines = fleet(gce)
    version = build_version(world, gce, machines)
    record = ManagedVolume.objects.get(build=version)
    run = queue_run(world)
    assert provisioner.tick(machines) == (1, 0)
    request = gce.requests[-1]
    instance = request["instance"]
    assert instance["scheduling"]["provisioningModel"] == "SPOT"
    launch = Launch.objects.get(setup=None)
    _, disk = instance["disks"]
    assert disk == {
        "deviceName": "iki-volume-1",
        "autoDelete": True,
        "initializeParams": {
            "diskName": f"{launch.machine}-d1",
            "diskSizeGb": str(3 * 1024),
            "diskType": f"zones/{request['zone']}/diskTypes/pd-balanced",
            "labels": {**instance["labels"], VOLUME_LABEL: "af-db"},
            "sourceImage": record.handle,
        },
        "diskEncryptionKey": {"kmsKeyName": KEY},
    }
    assert SETUP_LABEL not in instance["labels"]
    (given,) = gce.host(live(gce, launch))["volumes"]
    assert (given["device"], given["writable"]) == ("/dev/disk/by-id/google-iki-volume-1", False)
    provenance = {
        "af-db": {
            "build": str(version.pk),
            "hrid": version.hrid,
            "version": 1,
            "data_version": "2026-10",
        }
    }
    assert launch.volumes == provenance
    worker = WorkerClient(register(gce.token(live(gce, launch))).json()["token"])
    assert worker.lease().json()["volumes"] == provenance
    assert Job.objects.get(run_id=run["id"]).volumes == provenance


def test_a_retired_version_goes_once_no_machine_has_it(world: World) -> None:
    gce = FakeGce()
    machines = fleet(gce)
    first = build_version(world, gce, machines)
    record = ManagedVolume.objects.get(build=first)
    queue_run(world)
    provisioner.tick(machines)
    holder = Launch.objects.get(setup=None, state=LaunchState.STARTING)
    retired = api_as(world, "compute_admin").post(f"/compute/setups/{first.hrid}/retire")
    assert retired.status_code == 200, retired.content
    provisioner.tick(machines, later(6))
    (image,) = gce.images_made.values()
    assert image["status"] == "READY"  # a machine has a copy
    live(gce, holder)["status"] = "TERMINATED"
    provisioner.tick(machines, later(12))
    provisioner.tick(machines, later(18))
    assert image["status"] == "DELETED"
    record.refresh_from_db()
    assert record.state == VolumeState.DELETED


def test_disks_and_images_with_no_build_are_deleted_and_others_left(world: World) -> None:
    gce = FakeGce()
    machines = fleet(gce)
    stray = "0193a1b2-0000-7000-8000-000000000000"
    gce.disks_made[("us-central1-a", "iki-test-stray")] = {
        "name": "iki-test-stray",
        "zone": "us-central1-a",
        "status": "READY",
        "users": [],
        "labels": {OWNER_LABEL: "iki-test", SETUP_LABEL: stray},
    }
    gce.images_made["iki-test-stray"] = {
        "name": "iki-test-stray",
        "status": "READY",
        "labels": {OWNER_LABEL: "iki-test", SETUP_LABEL: "not-a-build"},
    }
    gce.images_made["theirs"] = {
        "name": "theirs",
        "status": "READY",
        "labels": {OWNER_LABEL: "someone-else", SETUP_LABEL: stray},
    }
    provisioner.tick(machines, timezone.now())
    assert gce.disks_made[("us-central1-a", "iki-test-stray")]["status"] == "DELETED"
    assert gce.images_made["iki-test-stray"]["status"] == "DELETED"
    assert gce.images_made["theirs"]["status"] == "READY"
