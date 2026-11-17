"""The vm tier on Compute Engine (ADR 0074, 0107): the provisioner starts an instance for
each queued run from the deployment's instance template, on Spot first and in each of its
template's zones, falling back to standard; its worker registers with a token made for
that machine alone; and what Compute Engine reports is squared with what Iki launched, a
Spot VM it takes back included. Compute Engine here is a fake that keeps what it's asked
for; :mod:`test_gce` checks the requests against google-cloud-compute's own types."""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from django.db.models import F
from django.utils import timezone
from org_world import World, build_world
from test_api_entities import api_as
from test_runs import TEMPLATE
from test_vm_tier import GI, IMAGE, WORKER_IMAGE, later, queue_run, register, use
from test_workers import WorkerClient

from iki_server.audit.models import AuditEvent
from iki_server.runs import catalog, provisioner, setups
from iki_server.runs.gce import LAUNCH_LABEL, OWNER_LABEL, GceError, startup_script
from iki_server.runs.gce_fleet import GceFleet
from iki_server.runs.models import ActionRun, CostRecord, Job, JobState, Launch, RunStatus, Worker

pytestmark = pytest.mark.django_db

PROJECT = "acme-iki"
SOURCE = "projects/acme-iki/regions/us-central1/instanceTemplates/iki-test-vm-20261116"
KEY = "projects/acme-iki/locations/us-central1/keyRings/iki-test/cryptoKeys/vm-disks"
FAMILY = "projects/cos-cloud/global/images/family/cos-stable"
COS = "https://www.googleapis.com/compute/v1/projects/cos-cloud/global/images/cos-stable-121"
ZONES = ["us-central1-a", "us-central1-b"]


def a100(**overrides: Any) -> dict[str, Any]:
    return {
        **TEMPLATE,
        "name": "a100-spot",
        "provisioner": "gcp_gce",
        "lifecycle": "ephemeral",
        "capacity": {
            "cpu": 96,
            "memory": 1360 * GI,
            "gpu_count": 8,
            "gpu_memory": 40 * GI,
            "scratch_disk": 8 * 1024 * GI,
        },
        "max_concurrent": 3,
        "price_per_hour": 9.83,
        "currency": "USD",
        "gcp": {
            "machine_type": "a2-highgpu-8g",
            "image": FAMILY,
            "spot": True,
            "fallback": "on_demand",
            "region": "us-central1",
            "zones": ZONES,
            "labels": {"cost-center": "rd-42"},
        },
        **overrides,
    }


@pytest.fixture
def world(settings: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[World]:
    settings.IKI_RUNS_WORKER_IMAGE = WORKER_IMAGE
    settings.IKI_RUNS_VM_OWNER = "iki-test"
    settings.IKI_RUNS_VM_CLOUD = "gcp"
    settings.IKI_RUNS_VM_GCP_PROJECT = PROJECT
    settings.IKI_RUNS_VM_GCP_INSTANCE_TEMPLATE = SOURCE
    settings.IKI_RUNS_VM_GCP_KMS_KEY = KEY
    settings.IKI_RUNS_VM_SERVER_URL = "https://iki.internal.example.com"
    use(monkeypatch, tmp_path, a100())
    # These are about the machines: volumes are test_vm_volumes_gce's.
    monkeypatch.setattr(setups, "filled", lambda environment: [])
    yield build_world()
    catalog.compute.cache_clear()


@pytest.fixture(autouse=True)
def keep_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    from iki_server.realtime import views

    monkeypatch.setattr(views, "close_old_connections", lambda: None)


class FakeGce:
    """Keeps the instances the provisioner starts, and their disks and the images made of
    them, as Compute Engine would."""

    def __init__(self) -> None:
        self.made: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        #: Compute Engine's answer to a try, by (model, zone), model, or "all": an error code.
        self.no: dict[Any, str] = {}
        self.deleted: list[str] = []
        self.consoles: dict[str, str] = {}
        #: Each instance's operations, by its name.
        self.operations: dict[str, list[str]] = {}
        self.properties: dict[str, Any] = {"networkInterfaces": [{"subnetwork": "nodes"}]}
        #: Volumes' disks, by their zone and name; images, by their name.
        self.disks_made: dict[tuple[str, str], dict[str, Any]] = {}
        self.images_made: dict[str, dict[str, Any]] = {}
        #: Whether instances are listed with their disks attached yet.
        self.listing_disks = True

    def insert(self, project: str, zone: str, request: dict[str, Any]) -> None:
        assert project == PROJECT
        self.requests.append({**request, "zone": zone})
        model = request["instance"]["scheduling"]["provisioningModel"]
        code = self.no.get((model, zone)) or self.no.get(model) or self.no.get("all")
        if code:
            raise GceError(code, f"No {model} capacity in {zone}.")
        name = request["instance"]["name"]
        assert name.startswith("iki-test-")
        self.made[name] = {
            **json.loads(json.dumps(request["instance"])),
            "zone": zone,
            "status": "PROVISIONING",
            "labelFingerprint": "f0",
            "request": request,
        }
        link = f"projects/{PROJECT}/zones/{zone}/instances/{name}"
        for disk in self.made[name]["disks"]:
            if disk.get("boot"):
                continue
            params = disk["initializeParams"]
            self.disks_made[(zone, params["diskName"])] = {
                "name": params["diskName"],
                "zone": zone,
                "status": "READY",
                "users": [link],
                "labels": params["labels"],
                "sourceImage": params.get("sourceImage"),
                "autoDelete": disk["autoDelete"],
            }
            disk["source"] = (
                f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/zones/{zone}"
                f"/disks/{params['diskName']}"
            )

    def instances(self, project: str, owner: str) -> list[dict[str, Any]]:
        found = [
            i
            for i in self.made.values()
            if i["status"] != "DELETED" and (i.get("labels") or {}).get(OWNER_LABEL) == owner
        ]
        if self.listing_disks:
            return found
        return [
            {**i, "disks": [{k: v for k, v in d.items() if k != "source"} for d in i["disks"]]}
            for i in found
        ]

    def delete(self, project: str, zone: str, name: str) -> None:
        if name in self.made:
            self.made[name]["status"] = "DELETED"
        self.deleted.append(name)
        # Its disks go with it, but those kept.
        for disk in self.disks_made.values():
            if any(user.endswith(f"/instances/{name}") for user in disk["users"]):
                disk["users"] = []
                if disk["autoDelete"]:
                    disk["status"] = "DELETED"

    def disks(self, project: str, owner: str) -> list[dict[str, Any]]:
        assert project == PROJECT
        return [
            d
            for d in self.disks_made.values()
            if d["status"] != "DELETED" and d["labels"].get(OWNER_LABEL) == owner
        ]

    def delete_disk(self, project: str, zone: str, name: str) -> None:
        disk = self.disks_made.get((zone, name))
        if disk is not None:
            assert not disk["users"], "Compute Engine won't delete a disk in use"
            disk["status"] = "DELETED"

    def create_image(self, project: str, request: dict[str, Any]) -> None:
        assert project == PROJECT
        image = request["image"]
        if image["name"] in self.images_made:
            return  # the same request again
        zone, name = image["sourceDisk"].split("/zones/", 1)[1].split("/disks/")
        assert not self.disks_made[(zone, name)]["users"], "a disk in use"
        self.images_made[image["name"]] = {
            **image,
            "status": "PENDING",
            "creationTimestamp": f"2026-11-17T00:00:{len(self.images_made):02}Z",
            "requestId": request["requestId"],
        }

    def images(self, project: str, owner: str) -> list[dict[str, Any]]:
        return [
            i
            for i in self.images_made.values()
            if i["status"] != "DELETED" and i["labels"].get(OWNER_LABEL) == owner
        ]

    def delete_image(self, project: str, name: str) -> None:
        if name in self.images_made:
            self.images_made[name]["status"] = "DELETED"

    def set_labels(
        self, project: str, zone: str, name: str, labels: dict[str, str], fingerprint: str
    ) -> None:
        assert fingerprint == self.made[name]["labelFingerprint"]
        self.made[name]["labels"] = dict(labels)

    def console(self, project: str, zone: str, name: str) -> str:
        return self.consoles.get(name, "")

    def history(self, project: str, zone: str, name: str) -> list[str]:
        return ["insert", *self.operations.get(name, [])]

    def template(self, project: str, source: str) -> dict[str, Any]:
        assert source == SOURCE
        return self.properties

    def image(self, project: str, image: str) -> str:
        return COS if image == FAMILY else image

    # What happens to the instances.

    def only(self) -> dict[str, Any]:
        live = [i for i in self.made.values() if i["status"] != "DELETED"]
        assert len(live) == 1, list(self.made)
        return live[0]

    def token(self, instance: dict[str, Any]) -> str:
        found = re.search(r"IKI_HOST_TOKEN'\n(.+)\nIKI_HOST_TOKEN", startup_script(instance))
        assert found is not None
        return found[1]

    def host(self, instance: dict[str, Any]) -> dict[str, Any]:
        found = re.search(r"IKI_HOST_JSON'\n(.+)\nIKI_HOST_JSON", startup_script(instance))
        assert found is not None
        config: dict[str, Any] = json.loads(found[1])
        return config


def fleet(gce: FakeGce) -> GceFleet:
    return GceFleet(gce)


# --- Starting machines -----------------------------------------------------------------------


def test_a_queued_run_gets_an_instance_that_runs_its_image_with_iki_worker(world: World) -> None:
    run = queue_run(world)
    gce = FakeGce()
    assert provisioner.tick(fleet(gce)) == (1, 0)
    instance = gce.only()
    request = instance["request"]
    launch = Launch.objects.get()
    assert (launch.template, launch.environment, launch.image, launch.namespace) == (
        "a100-spot",
        "structure_prediction.alphafold-gpu",
        IMAGE,
        "us-central1",
    )
    assert (launch.machine, launch.market, launch.zone) == (
        instance["name"],
        "spot",
        instance["zone"],
    )
    # Named for the deployment, as the provisioner's IAM condition requires.
    assert launch.machine == f"iki-test-run-{launch.pk.hex}"
    assert launch.message.startswith("Starting a Spot a2-highgpu-8g in us-central1-")
    # From the deployment's instance template, Shielded, Spot, its disk encrypted.
    assert request["sourceInstanceTemplate"] == SOURCE
    assert instance["shieldedInstanceConfig"]["enableSecureBoot"] is True
    assert instance["scheduling"]["provisioningModel"] == "SPOT"
    (disk,) = instance["disks"]
    assert disk["diskEncryptionKey"] == {"kmsKeyName": KEY}
    # The image its family names now.
    assert disk["initializeParams"]["sourceImage"] == COS
    labels = instance["labels"]
    assert labels[OWNER_LABEL] == "iki-test"
    assert labels[LAUNCH_LABEL] == str(launch.pk)
    assert labels["cost-center"] == "rd-42"
    host = gce.host(instance)
    assert (host["image"], host["once"], host["spot"], host["gpus"]) == (IMAGE, True, True, 8)
    assert host["cloud"] == "gcp"
    assert host["env"]["IKI_WORKER_NAME"] == launch.name
    token = gce.token(instance)
    assert token.startswith("iki_wlch_")
    assert token not in json.dumps(host)
    started = AuditEvent.objects.get(action="worker.machine_started")
    assert started.data["instance"] == instance["name"]
    assert started.data["resolved_image"] == COS
    assert started.data["instance_template"] == SOURCE
    assert token not in json.dumps(started.data)
    # A machine is already starting for it.
    assert provisioner.tick(fleet(gce)) == (0, 0)
    assert ActionRun.objects.get(pk=run["id"]).status == RunStatus.QUEUED


def test_no_spot_capacity_tries_each_zone_then_standard(world: World) -> None:
    queue_run(world)
    gce = FakeGce()
    gce.no["SPOT"] = "ZONE_RESOURCE_POOL_EXHAUSTED"
    assert provisioner.tick(fleet(gce)) == (1, 0)
    tries = [(r["instance"]["scheduling"]["provisioningModel"], r["zone"]) for r in gce.requests]
    assert [m for m, _ in tries] == ["SPOT", "SPOT", "STANDARD"]
    assert {z for _, z in tries[:2]} == set(ZONES)
    # Each try is its own, so Compute Engine never starts two machines for one.
    assert len({r["requestId"] for r in gce.requests}) == 3
    assert Launch.objects.get().market == "on_demand"


def test_without_fallback_or_capacity_runs_wait_and_say_why(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use(monkeypatch, tmp_path, a100(gcp={**a100()["gcp"], "fallback": "none"}))
    run = queue_run(world)
    gce = FakeGce()
    gce.no["all"] = "QUOTA_EXCEEDED"
    assert provisioner.tick(fleet(gce)) == (0, 0)
    launch = Launch.objects.get()
    assert launch.ended_reason == "no_capacity"
    assert "Compute Engine has no a2-highgpu-8g free" in launch.message
    assert "QUOTA_EXCEEDED" in launch.message
    assert len(gce.requests) == 2  # Spot in each zone, and no standard
    shown = api_as(world, "runner").get(f"/runs/{run['hrid']}").json()
    assert "Compute Engine has no a2-highgpu-8g free" in shown["waiting"]
    # It backs off before trying again.
    assert provisioner.tick(fleet(gce)) == (0, 0)
    assert len(gce.requests) == 2
    gce.no.clear()
    assert provisioner.tick(fleet(gce), later(1)) == (1, 0)


def test_when_compute_engine_keeps_refusing_the_runs_fail_saying_what_to_check(
    world: World,
) -> None:
    from datetime import timedelta

    run = queue_run(world)
    gce = FakeGce()
    gce.no["all"] = "forbidden"
    for attempt in range(provisioner.GIVE_UP_AFTER):
        assert provisioner.tick(fleet(gce), later(attempt * 20)) == (0, 0), attempt
        Launch.objects.update(ended_at=F("ended_at") - timedelta(minutes=5))
    assert len(gce.requests) == provisioner.GIVE_UP_AFTER  # never another zone or model
    failed = ActionRun.objects.get(pk=run["id"])
    assert failed.status == RunStatus.FAILED
    assert failed.error is not None
    assert failed.error["code"] == "machine_unavailable"
    assert "service account may start the template's instances" in failed.error["message"]


def test_an_instance_template_with_a_public_address_starts_nothing(world: World) -> None:
    queue_run(world)
    gce = FakeGce()
    gce.properties = {"networkInterfaces": [{"accessConfigs": [{"type": "ONE_TO_ONE_NAT"}]}]}
    assert provisioner.tick(fleet(gce)) == (0, 0)
    assert not gce.requests
    launch = Launch.objects.get()
    assert launch.ended_reason == "refused"
    assert "public address" in launch.message


def test_no_more_machines_start_than_the_template_allows(world: World) -> None:
    for i in range(5):
        queue_run(world, name="ACDEFGHIK"[: i + 1])
    gce = FakeGce()
    assert provisioner.tick(fleet(gce)) == (3, 0)
    assert provisioner.tick(fleet(gce)) == (0, 0)
    assert len(gce.made) == 3
    # Spread over the template's zones.
    assert len({i["zone"] for i in gce.made.values()}) == 2


# --- Running on one --------------------------------------------------------------------------


def test_its_worker_runs_one_vm_job_and_the_machine_ends_itself(world: World) -> None:
    run = queue_run(world)
    gce = FakeGce()
    provisioner.tick(fleet(gce))
    instance = gce.only()
    instance["status"] = "RUNNING"
    made = register(gce.token(instance))
    assert made.status_code == 201, made.content
    assert Worker.objects.get(pk=made.json()["id"]).tier == "vm"
    machine = WorkerClient(made.json()["token"])
    leased = machine.lease()
    assert leased.status_code == 200, leased.content
    # Labelled with whose run it is, for billing reports.
    provisioner.tick(fleet(gce))
    assert instance["labels"]["iki-run"] == run["id"]
    assert instance["labels"]["iki-project"] == str(world.project.pk)
    assert instance["labels"][OWNER_LABEL] == "iki-test"  # the rest kept
    # Its run done, the host shuts the machine down: it's stopped, and Iki deletes it.
    machine.report(leased.json(), "complete", outputs={})
    instance["status"] = "TERMINATED"
    assert provisioner.tick(fleet(gce)) == (0, 1)
    launch = Launch.objects.get()
    assert launch.ended_reason == "finished"
    assert instance["name"] in gce.deleted
    assert CostRecord.objects.filter(launch=launch).exists()


def test_a_spot_vm_compute_engine_takes_back_retries_its_run(world: World) -> None:
    run = queue_run(world)
    gce = FakeGce()
    provisioner.tick(fleet(gce))
    instance = gce.only()
    machine = WorkerClient(register(gce.token(instance)).json()["token"])
    assert machine.lease().status_code == 200
    # The host saw the preemption notice and stopped the worker (which saved the run's
    # checkpoint); then Compute Engine deleted the instance, as its termination action says.
    gce.operations[instance["name"]] = ["compute.instances.preempted"]
    instance["status"] = "DELETED"
    started, ended = provisioner.tick(fleet(gce), later(2))
    assert ended == 1
    first = Launch.objects.get(machine=instance["name"])
    assert (first.ended_reason, first.message) == (
        "reclaimed",
        "Compute Engine took its Spot VM back.",
    )
    assert Job.objects.get(run_id=run["id"], attempt=1).state == JobState.INTERRUPTED
    assert Job.objects.get(run_id=run["id"], attempt=2).state == JobState.QUEUED
    assert started == 1


def test_a_machine_whose_host_gave_up_says_why(world: World) -> None:
    queue_run(world)
    gce = FakeGce()
    provisioner.tick(fleet(gce))
    instance = gce.only()
    gce.consoles[instance["name"]] = (
        "google_metadata_script_runner: startup-script: running\n"
        f"IKI-HOST-ERROR image_unavailable: couldn't pull {IMAGE}: manifest unknown\n"
    )
    instance["status"] = "STOPPING"
    assert provisioner.tick(fleet(gce), later(2))[1] == 1
    launch = Launch.objects.get(machine=instance["name"])
    assert launch.ended_reason == "image_unavailable"
    assert "manifest unknown" in launch.message
    assert instance["name"] in gce.deleted


def test_one_that_shut_down_saying_nothing_says_where_to_look(world: World) -> None:
    queue_run(world)
    gce = FakeGce()
    provisioner.tick(fleet(gce))
    instance = gce.only()
    instance["status"] = "TERMINATED"
    provisioner.tick(fleet(gce), later(2))
    launch = Launch.objects.get(machine=instance["name"])
    assert launch.ended_reason == "failed"
    assert "serial port" in launch.message


def test_a_machine_whose_worker_never_registers_is_deleted(world: World) -> None:
    from datetime import timedelta

    queue_run(world)
    gce = FakeGce()
    provisioner.tick(fleet(gce))
    instance = gce.only()
    instance["status"] = "RUNNING"
    Launch.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert provisioner.tick(fleet(gce), later(1)) == (1, 1)
    assert instance["name"] in gce.deleted
    assert Launch.objects.get(machine=instance["name"]).ended_reason == "never_started"


def test_instances_with_no_live_launch_are_deleted_and_others_left_alone(world: World) -> None:
    from iki_server.runs.models import LaunchState

    queue_run(world)
    gce = FakeGce()
    provisioner.tick(fleet(gce))
    instance = gce.only()
    Launch.objects.update(state=LaunchState.ENDED, ended_at=timezone.now(), ended_reason="removed")
    theirs = dict(instance, name="theirs-run-1", status="RUNNING")
    theirs["labels"] = {OWNER_LABEL: "someone-else"}
    gce.made["theirs-run-1"] = theirs
    provisioner.tick(fleet(gce))
    assert instance["name"] in gce.deleted
    assert "theirs-run-1" not in gce.deleted


def test_an_instance_stopped_outside_iki_ends_its_launch(world: World) -> None:
    run = queue_run(world)
    gce = FakeGce()
    provisioner.tick(fleet(gce))
    instance = gce.only()
    machine = WorkerClient(register(gce.token(instance)).json()["token"])
    assert machine.lease().status_code == 200
    gce.operations[instance["name"]] = ["stop"]
    instance["status"] = "TERMINATED"
    provisioner.tick(fleet(gce), later(2))
    launch = Launch.objects.get(machine=instance["name"])
    assert launch.ended_reason == "gone"
    # A stopped instance still costs its disk: it's deleted.
    assert instance["name"] in gce.deleted
    assert Job.objects.get(run_id=run["id"], attempt=1).state == JobState.INTERRUPTED


# --- Warm machines ---------------------------------------------------------------------------


def test_persistent_machines_are_kept_warm_and_stopped_once_idle(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    warm = a100(lifecycle="persistent", min_warm=1, idle_shutdown=600, name="a100-warm")
    use(monkeypatch, tmp_path, warm)
    gce = FakeGce()
    assert provisioner.tick(fleet(gce)) == (1, 0)
    instance = gce.only()
    assert gce.host(instance)["once"] is False
    assert register(gce.token(instance)).status_code == 201
    instance["status"] = "RUNNING"
    assert provisioner.tick(fleet(gce), later(30)) == (0, 0)
    use(monkeypatch, tmp_path, {**warm, "min_warm": 0})
    assert provisioner.tick(fleet(gce), later(30)) == (0, 1)
    assert instance["name"] in gce.deleted
    assert Launch.objects.get().ended_reason == "idle"


def test_a_warm_machine_is_replaced_once_idle_when_its_template_changes(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    warm = a100(lifecycle="persistent", min_warm=1, name="a100-warm")
    use(monkeypatch, tmp_path, warm)
    gce = FakeGce()
    provisioner.tick(fleet(gce))
    old = gce.only()
    register(gce.token(old))
    old["status"] = "RUNNING"
    # The same template, ticked again, is the same machine.
    assert provisioner.tick(fleet(gce)) == (0, 0)
    use(monkeypatch, tmp_path, {**warm, "gcp": {**warm["gcp"], "machine_type": "a3-highgpu-8g"}})
    assert provisioner.tick(fleet(gce)) == (1, 1)
    assert old["name"] in gce.deleted
    assert gce.only()["machineType"].endswith("/a3-highgpu-8g")
    assert Launch.objects.get(machine=old["name"]).ended_reason == "replaced"


def test_the_provisioner_starts_compute_engines_fleet_when_its_settings_say(
    settings: Any,
) -> None:
    from django.core.management.base import CommandError

    from iki_server.runs.management.commands.provision import fleets

    settings.IKI_RUNS_NAMESPACE = ""
    settings.IKI_RUNS_VM_OWNER = "iki-test"
    settings.IKI_RUNS_VM_SERVER_URL = "https://iki.internal.example.com"
    settings.IKI_RUNS_WORKER_IMAGE = WORKER_IMAGE
    settings.IKI_RUNS_VM_CLOUD = "gcp"
    settings.IKI_RUNS_VM_GCP_PROJECT = PROJECT
    settings.IKI_RUNS_VM_GCP_INSTANCE_TEMPLATE = ""
    with pytest.raises(CommandError, match="IKI_RUNS_VM_GCP_INSTANCE_TEMPLATE"):
        fleets()
    settings.IKI_RUNS_VM_GCP_INSTANCE_TEMPLATE = SOURCE
    (only,) = fleets()
    assert isinstance(only, GceFleet)
    assert only.project == PROJECT


def test_runs_wait_for_volumes_their_template_cant_give(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setups, "filled", lambda environment: ["af-db"])
    run = queue_run(world)
    gce = FakeGce()
    assert provisioner.tick(fleet(gce)) == (0, 0)
    assert not gce.requests
    shown = api_as(world, "runner").get(f"/runs/{run['hrid']}").json()
    assert shown["waiting"].startswith("Waiting for its reference data (af-db)")
