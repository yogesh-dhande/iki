"""The vm tier (ADR 0074): the provisioner starts an EC2 instance for each queued run, on spot
first and in each of its template's zones, falling back to on-demand; its worker registers
with a token made for that machine alone; and what EC2 reports is squared with what Iki
launched, a spot instance AWS takes back included. EC2 here is a fake that keeps what it's
asked for; :mod:`test_ec2` checks the requests against EC2's own API model."""

import json
import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from django.db.models import F
from django.utils import timezone
from org_world import World, build_world
from test_api_entities import api_as
from test_runs import TEMPLATE, protein
from test_workers import WorkerClient

from iki_server.audit.models import AuditEvent
from iki_server.runs import catalog, provisioner, setups
from iki_server.runs.ec2 import LAUNCH_TAG, OWNER_TAG, Ec2Error
from iki_server.runs.ec2_fleet import Ec2Fleet
from iki_server.runs.models import (
    ActionRun,
    CostRecord,
    Job,
    JobState,
    Launch,
    LaunchState,
    RunStatus,
    Worker,
)

pytestmark = pytest.mark.django_db

PREDICT = "structure_prediction.predict_structure"
FILLED = setups.filled
IMAGE = (
    "ghcr.io/acme/alphafold@sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
)
WORKER_IMAGE = "ghcr.io/yogesh-dhande/iki-worker:0.1.0@sha256:" + "b" * 64
GI = 1024**3
SUBNETS = ["subnet-0a1b2c3d4e5f60718", "subnet-0f1e2d3c4b5a69788"]
ZONES = dict(zip(SUBNETS, ["us-east-1a", "us-east-1b"], strict=True))
AMI = "resolve:ssm:/aws/service/ecs/optimized-ami/amazon-linux-2023/gpu/recommended/image_id"


def a100(**overrides: Any) -> dict[str, Any]:
    return {
        **TEMPLATE,
        "name": "a100-spot",
        "provisioner": "aws_ec2",
        "lifecycle": "ephemeral",
        "capacity": {
            "cpu": 96,
            "memory": 1152 * GI,
            "gpu_count": 8,
            "gpu_memory": 40 * GI,
            "scratch_disk": 8 * 1024 * GI,
        },
        "max_concurrent": 3,
        "price_per_hour": 9.83,
        "currency": "USD",
        "aws": {
            "instance_type": "p4d.24xlarge",
            "image": AMI,
            "spot": True,
            "fallback": "on_demand",
            "region": "us-east-1",
            "subnets": SUBNETS,
            "security_groups": ["sg-0a1b2c3d4e5f60718"],
            "instance_profile": "iki-vm-worker",
            "kms_key": "alias/iki-runs",
            "tags": {"cost-center": "rd-42"},
        },
        **overrides,
    }


def use(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *templates: dict[str, Any]) -> None:
    compute = json.loads(Path(catalog._compute_file()).read_text())  # type: ignore[arg-type]
    for template in templates:
        compute["templates"][template["name"]] = template
    compute["bindings"]["structure_prediction.alphafold-gpu"] = {
        "environment": "structure_prediction.alphafold-gpu",
        "templates": [[t["name"], i] for i, t in enumerate(templates)],
    }
    file = tmp_path / "with-vms.json"
    file.write_text(json.dumps(compute))
    monkeypatch.setenv("IKI_COMPUTE_FILE", str(file))
    catalog.compute.cache_clear()


@pytest.fixture
def world(settings: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[World]:
    settings.IKI_RUNS_WORKER_IMAGE = WORKER_IMAGE
    settings.IKI_RUNS_VM_OWNER = "iki-test"
    settings.IKI_RUNS_VM_SERVER_URL = "https://iki.internal.example.com"
    use(monkeypatch, tmp_path, a100())
    # These are about the machines: AlphaFold's databases (a volume its setup fills) come
    # to vm-tier machines with EBS snapshots (step 36b). Until then its runs wait for them:
    # test_vm_machines_wait_for_volumes_they_cant_have_yet.
    monkeypatch.setattr(setups, "filled", lambda environment: [])
    yield build_world()
    catalog.compute.cache_clear()


@pytest.fixture(autouse=True)
def keep_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    from iki_server.realtime import views

    monkeypatch.setattr(views, "close_old_connections", lambda: None)


def tag(instance: dict[str, Any], key: str) -> str | None:
    return next((t["Value"] for t in instance.get("Tags") or [] if t["Key"] == key), None)


class FakeEc2:
    """Keeps the instances the provisioner starts, as EC2 would."""

    def __init__(self) -> None:
        self.made: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        #: EC2's answer to a try, by (market, subnet), market, or "all": an error code.
        self.no: dict[Any, str] = {}
        self.terminated: list[str] = []
        self.consoles: dict[str, str] = {}

    def run_instance(self, region: str, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        market = "spot" if request.get("InstanceMarketOptions") else "on_demand"
        subnet = request["NetworkInterfaces"][0]["SubnetId"]
        code = self.no.get((market, subnet)) or self.no.get(market) or self.no.get("all")
        if code:
            raise Ec2Error(code, f"No {market} capacity in {subnet}.")
        (tags,) = [
            t["Tags"] for t in request["TagSpecifications"] if t["ResourceType"] == "instance"
        ]
        instance = {
            "InstanceId": f"i-{len(self.made) + 1:017x}",
            "ImageId": "ami-0123456789abcdef0",
            "SubnetId": subnet,
            "Placement": {"AvailabilityZone": ZONES[subnet]},
            "State": {"Name": "pending"},
            "Tags": list(tags),
            "region": region,
            "request": request,
        }
        self.made[instance["InstanceId"]] = instance
        return instance

    def instances(self, region: str, owner: str) -> list[dict[str, Any]]:
        return [
            i for i in self.made.values() if i["region"] == region and tag(i, OWNER_TAG) == owner
        ]

    def terminate(self, region: str, instance_ids: list[str]) -> None:
        for each in instance_ids:
            self.end(each, "Client.UserInitiatedShutdown")
            self.terminated.append(each)

    def tag(self, region: str, instance_id: str, tags: dict[str, str], remove: list[str]) -> None:
        instance = self.made[instance_id]
        kept = [t for t in instance["Tags"] if t["Key"] not in tags and t["Key"] not in remove]
        instance["Tags"] = kept + [{"Key": k, "Value": v} for k, v in tags.items()]

    def console(self, region: str, instance_id: str) -> str:
        return self.consoles.get(instance_id, "")

    # What happens to the instances.

    def only(self) -> dict[str, Any]:
        live = [i for i in self.made.values() if i["State"]["Name"] != "terminated"]
        assert len(live) == 1, list(self.made)
        return live[0]

    def end(self, instance_id: str, code: str, state: str = "terminated") -> None:
        instance = self.made[instance_id]
        instance["State"] = {"Name": state}
        instance["StateReason"] = {"Code": code, "Message": f"{code}: it ended"}

    def token(self, instance: dict[str, Any]) -> str:
        found = re.search(r"IKI_HOST_TOKEN'\n(.+)\nIKI_HOST_TOKEN", instance["request"]["UserData"])
        assert found is not None
        return found[1]

    def host(self, instance: dict[str, Any]) -> dict[str, Any]:
        found = re.search(r"IKI_HOST_JSON'\n(.+)\nIKI_HOST_JSON", instance["request"]["UserData"])
        assert found is not None
        config: dict[str, Any] = json.loads(found[1])
        return config


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


def register(token: str) -> Any:
    return WorkerClient(token).post(
        "/register",
        {
            "name": "iki-run-vm",
            "version": "0.1.0",
            "capacity": {"cpu": 2, "memory": 4 * GI},
            "plugins": [{"name": "dna", "sha": None, "environments": None}],
        },
    )


def later(minutes: float) -> datetime:
    return timezone.now() + timedelta(minutes=minutes)


def fleet(ec2: FakeEc2) -> Ec2Fleet:
    return Ec2Fleet(ec2)


# --- Starting machines -----------------------------------------------------------------------


def test_a_queued_run_gets_an_instance_that_runs_its_image_with_iki_worker(world: World) -> None:
    run = queue_run(world)
    ec2 = FakeEc2()
    assert provisioner.tick(fleet(ec2)) == (1, 0)
    instance = ec2.only()
    request = instance["request"]
    launch = Launch.objects.get()
    assert (launch.template, launch.environment, launch.image, launch.namespace) == (
        "a100-spot",
        "structure_prediction.alphafold-gpu",
        IMAGE,
        "us-east-1",
    )
    assert (launch.machine, launch.market, launch.zone) == (
        instance["InstanceId"],
        "spot",
        ZONES[instance["SubnetId"]],
    )
    assert launch.message.startswith("Starting a spot p4d.24xlarge in us-east-1")
    # The template's machine, on spot, in a private subnet with no public address.
    assert (request["ImageId"], request["InstanceType"]) == (AMI, "p4d.24xlarge")
    assert request["InstanceMarketOptions"]["MarketType"] == "spot"
    (nic,) = request["NetworkInterfaces"]
    assert nic["AssociatePublicIpAddress"] is False
    assert nic["Groups"] == ["sg-0a1b2c3d4e5f60718"]
    assert request["IamInstanceProfile"] == {"Name": "iki-vm-worker"}
    # Only the machine itself reaches its metadata: IMDSv2, one hop.
    assert request["MetadataOptions"]["HttpTokens"] == "required"
    assert request["MetadataOptions"]["HttpPutResponseHopLimit"] == 1
    # An encrypted disk with room for the scratch space, gone with the machine.
    (disk,) = request["BlockDeviceMappings"]
    assert disk["Ebs"] == {
        "VolumeSize": 8 * 1024 + 40,
        "VolumeType": "gp3",
        "Encrypted": True,
        "DeleteOnTermination": True,
        "KmsKeyId": "alias/iki-runs",
    }
    assert request["InstanceInitiatedShutdownBehavior"] == "terminate"
    assert tag(instance, OWNER_TAG) == "iki-test"
    assert tag(instance, LAUNCH_TAG) == str(launch.pk)
    assert tag(instance, "Name") == launch.name
    assert tag(instance, "cost-center") == "rd-42"
    # The host runs the environment's image with the worker, one job, as the template says.
    host = ec2.host(instance)
    assert (host["image"], host["once"], host["spot"], host["gpus"]) == (IMAGE, True, True, 8)
    assert host["env"]["IKI_URL"] == "https://iki.internal.example.com"
    assert host["env"]["IKI_WORKER_ENVIRONMENT"] == "structure_prediction.alphafold-gpu"
    assert host["env"]["IKI_WORKER_NAME"] == launch.name
    assert WORKER_IMAGE in request["UserData"]
    token = ec2.token(instance)
    assert token.startswith("iki_wlch_")
    assert token not in json.dumps(host)
    started = AuditEvent.objects.get(action="worker.machine_started")
    assert started.data["instance"] == instance["InstanceId"]
    assert started.data["resolved_image"] == "ami-0123456789abcdef0"
    assert token not in json.dumps(started.data)
    # A machine is already starting for it.
    assert provisioner.tick(fleet(ec2)) == (0, 0)
    assert ActionRun.objects.get(pk=run["id"]).status == RunStatus.QUEUED


def test_no_spot_capacity_tries_each_zone_then_on_demand(world: World) -> None:
    queue_run(world)
    ec2 = FakeEc2()
    ec2.no["spot"] = "InsufficientInstanceCapacity"
    assert provisioner.tick(fleet(ec2)) == (1, 0)
    markets = [
        (
            "spot" if r.get("InstanceMarketOptions") else "on_demand",
            r["NetworkInterfaces"][0]["SubnetId"],
        )
        for r in ec2.requests
    ]
    assert [m for m, _ in markets] == ["spot", "spot", "on_demand"]
    assert {s for _, s in markets[:2]} == set(SUBNETS)
    # Each try is its own, so EC2 never starts two machines for one.
    assert len({r["ClientToken"] for r in ec2.requests}) == 3
    assert Launch.objects.get().market == "on_demand"


def test_without_fallback_or_capacity_runs_wait_and_say_why(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use(monkeypatch, tmp_path, a100(aws={**a100()["aws"], "fallback": "none"}))
    run = queue_run(world)
    ec2 = FakeEc2()
    ec2.no["all"] = "InsufficientInstanceCapacity"
    assert provisioner.tick(fleet(ec2)) == (0, 0)
    launch = Launch.objects.get()
    assert (launch.state, launch.ended_reason) == (LaunchState.ENDED, "no_capacity")
    assert "EC2 has no p4d.24xlarge free" in launch.message
    assert len(ec2.requests) == 2  # spot in each zone, and no on-demand
    shown = api_as(world, "runner").get(f"/runs/{run['hrid']}").json()
    assert "EC2 has no p4d.24xlarge free" in shown["waiting"]
    # It backs off before trying again, and the run stays queued.
    assert provisioner.tick(fleet(ec2)) == (0, 0)
    assert len(ec2.requests) == 2
    assert ActionRun.objects.get(pk=run["id"]).status == RunStatus.QUEUED
    ec2.no.clear()
    assert provisioner.tick(fleet(ec2), later(1)) == (1, 0)


def test_vm_machines_wait_for_volumes_they_cant_have_yet(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setups, "filled", FILLED)
    run = queue_run(world)
    ec2 = FakeEc2()
    assert provisioner.tick(fleet(ec2)) == (0, 0)
    assert not ec2.requests  # no machine without AlphaFold's databases
    shown = api_as(world, "runner").get(f"/runs/{run['hrid']}").json()
    assert shown["waiting"].startswith("Waiting for its reference data (af-db)")


def test_when_ec2_keeps_refusing_the_runs_fail_saying_what_to_check(world: World) -> None:
    run = queue_run(world)
    ec2 = FakeEc2()
    ec2.no["all"] = "UnauthorizedOperation"
    for attempt in range(provisioner.GIVE_UP_AFTER):
        assert provisioner.tick(fleet(ec2), later(attempt * 20)) == (0, 0), attempt
        Launch.objects.update(ended_at=F("ended_at") - timedelta(minutes=5))
    assert len(ec2.requests) == provisioner.GIVE_UP_AFTER  # never another zone or market
    failed = ActionRun.objects.get(pk=run["id"])
    assert failed.status == RunStatus.FAILED
    assert failed.error is not None
    assert failed.error["code"] == "machine_unavailable"
    assert "IAM role may start the template's instances" in failed.error["message"]
    assert "UnauthorizedOperation" in failed.error["message"]


def test_no_more_machines_start_than_the_template_allows(world: World) -> None:
    for i in range(5):
        queue_run(world, name="ACDEFGHIK"[: i + 1])
    ec2 = FakeEc2()
    assert provisioner.tick(fleet(ec2)) == (3, 0)
    assert provisioner.tick(fleet(ec2)) == (0, 0)
    assert len(ec2.made) == 3
    # Spread over the template's zones.
    assert len({i["SubnetId"] for i in ec2.made.values()}) == 2


# --- Running on one --------------------------------------------------------------------------


def test_its_worker_runs_one_vm_job_and_the_machine_ends_itself(world: World) -> None:
    run = queue_run(world)
    ec2 = FakeEc2()
    provisioner.tick(fleet(ec2))
    instance = ec2.only()
    instance["State"] = {"Name": "running"}
    made = register(ec2.token(instance))
    assert made.status_code == 201, made.content
    worker = Worker.objects.get(pk=made.json()["id"])
    assert worker.tier == "vm"
    assert worker.capacity["gpu_count"] == 8  # the template's, whatever it said
    machine = WorkerClient(made.json()["token"])
    leased = machine.lease()
    assert leased.status_code == 200, leased.content
    job = Job.objects.get(pk=leased.json()["job"]["id"])
    assert (str(job.run_id), job.template) == (run["id"], "a100-spot")
    # Tagged with whose run it is, for AWS's cost reports.
    provisioner.tick(fleet(ec2))
    assert tag(instance, "iki.ikisci.org/run") == run["id"]
    assert tag(instance, "iki.ikisci.org/project") == str(world.project.pk)
    # Its run done, the host shuts the machine down, and EC2 terminates it.
    machine.report(leased.json(), "complete", outputs={})
    ec2.end(instance["InstanceId"], "Client.InstanceInitiatedShutdown")
    assert provisioner.tick(fleet(ec2)) == (0, 1)
    launch = Launch.objects.get()
    assert (launch.state, launch.ended_reason) == (LaunchState.ENDED, "finished")
    assert CostRecord.objects.filter(launch=launch).exists()


def test_a_spot_machine_aws_takes_back_retries_its_run(world: World) -> None:
    run = queue_run(world)
    ec2 = FakeEc2()
    provisioner.tick(fleet(ec2))
    instance = ec2.only()
    machine = WorkerClient(register(ec2.token(instance)).json()["token"])
    assert machine.lease().status_code == 200
    # The host saw AWS's two-minute notice, stopped the worker (which saved the run's
    # checkpoint), and then the instance went.
    ec2.end(instance["InstanceId"], "Server.SpotInstanceTermination")
    started, ended = provisioner.tick(fleet(ec2), later(2))
    assert ended == 1
    first = Launch.objects.get(machine=instance["InstanceId"])
    assert (first.ended_reason, first.message) == ("reclaimed", "AWS took its spot instance back.")
    attempt = Job.objects.get(run_id=run["id"], attempt=1)
    assert attempt.state == JobState.INTERRUPTED
    # Retried on another machine, as the action's retries allow.
    assert Job.objects.get(run_id=run["id"], attempt=2).state == JobState.QUEUED
    assert started == 1
    assert len([i for i in ec2.made.values() if i["State"]["Name"] != "terminated"]) == 1


def test_a_machine_whose_host_gave_up_says_why(world: World) -> None:
    queue_run(world)
    ec2 = FakeEc2()
    provisioner.tick(fleet(ec2))
    instance = ec2.only()
    ec2.consoles[instance["InstanceId"]] = (
        "[   12.3] cloud-init: running user data\n"
        f"IKI-HOST-ERROR image_unavailable: couldn't pull {IMAGE}: manifest unknown\n"
    )
    ec2.end(instance["InstanceId"], "Client.InstanceInitiatedShutdown")
    assert provisioner.tick(fleet(ec2), later(2))[1] == 1
    launch = Launch.objects.get(machine=instance["InstanceId"])
    assert launch.ended_reason == "image_unavailable"
    assert "manifest unknown" in launch.message


def test_a_machine_whose_worker_never_registers_is_terminated(world: World) -> None:
    queue_run(world)
    ec2 = FakeEc2()
    provisioner.tick(fleet(ec2))
    instance = ec2.only()
    Launch.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert provisioner.tick(fleet(ec2), later(1)) == (1, 1)
    assert instance["InstanceId"] in ec2.terminated
    assert Launch.objects.get(machine=instance["InstanceId"]).ended_reason == "never_started"


def test_instances_with_no_live_launch_are_terminated_and_others_left_alone(
    world: World,
) -> None:
    queue_run(world)
    ec2 = FakeEc2()
    provisioner.tick(fleet(ec2))
    instance = ec2.only()
    # Its launch ended (cancelled, say) and another deployment's instance in the account.
    Launch.objects.update(state=LaunchState.ENDED, ended_at=timezone.now(), ended_reason="removed")
    theirs = dict(instance, InstanceId="i-0ffffffffffffffff", State={"Name": "running"})
    theirs["Tags"] = [{"Key": OWNER_TAG, "Value": "someone-else"}]
    ec2.made[theirs["InstanceId"]] = theirs
    provisioner.tick(fleet(ec2))
    assert instance["InstanceId"] in ec2.terminated
    assert "i-0ffffffffffffffff" not in ec2.terminated


def test_an_instance_stopped_outside_iki_ends_its_launch(world: World) -> None:
    run = queue_run(world)
    ec2 = FakeEc2()
    provisioner.tick(fleet(ec2))
    instance = ec2.only()
    machine = WorkerClient(register(ec2.token(instance)).json()["token"])
    assert machine.lease().status_code == 200
    ec2.end(instance["InstanceId"], "Client.UserInitiatedShutdown", state="stopped")
    provisioner.tick(fleet(ec2), later(2))
    launch = Launch.objects.get(machine=instance["InstanceId"])
    assert launch.ended_reason == "gone"
    # A stopped instance still costs its disk: it's terminated.
    assert instance["InstanceId"] in ec2.terminated
    assert Job.objects.get(run_id=run["id"], attempt=1).state == JobState.INTERRUPTED


# --- Warm machines ---------------------------------------------------------------------------


def test_persistent_machines_are_kept_warm_and_stopped_once_idle(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    warm = a100(lifecycle="persistent", min_warm=1, idle_shutdown=600, name="a100-warm")
    use(monkeypatch, tmp_path, warm)
    ec2 = FakeEc2()
    assert provisioner.tick(fleet(ec2)) == (1, 0)
    instance = ec2.only()
    host = ec2.host(instance)
    # It runs one job after another, cleaning up between them (ADR 0062).
    assert host["once"] is False
    assert host["env"]["IKI_WORKER_CLEAN_DIR"] == "/iki/scratch"
    assert "IKI_WORKER_IDLE_EXIT" not in host["env"]
    assert register(ec2.token(instance)).status_code == 201
    instance["State"] = {"Name": "running"}
    # min_warm keeps it, however long it waits.
    assert provisioner.tick(fleet(ec2), later(30)) == (0, 0)
    # Without it, it's stopped once it has had nothing to do for idle_shutdown.
    use(monkeypatch, tmp_path, {**warm, "min_warm": 0})
    assert provisioner.tick(fleet(ec2), later(30)) == (0, 1)
    assert instance["InstanceId"] in ec2.terminated
    assert Launch.objects.get().ended_reason == "idle"


def test_a_warm_machine_is_replaced_once_idle_when_its_template_changes(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    warm = a100(lifecycle="persistent", min_warm=1, name="a100-warm")
    use(monkeypatch, tmp_path, warm)
    ec2 = FakeEc2()
    provisioner.tick(fleet(ec2))
    old = ec2.only()
    register(ec2.token(old))
    use(monkeypatch, tmp_path, {**warm, "aws": {**warm["aws"], "instance_type": "p5.48xlarge"}})
    assert provisioner.tick(fleet(ec2)) == (1, 1)
    assert old["InstanceId"] in ec2.terminated
    assert ec2.only()["request"]["InstanceType"] == "p5.48xlarge"
    assert Launch.objects.get(machine=old["InstanceId"]).ended_reason == "replaced"


def test_the_provisioner_starts_the_fleets_its_settings_ask_for(settings: Any) -> None:
    from django.core.management.base import CommandError

    from iki_server.runs.management.commands.provision import fleets

    settings.IKI_RUNS_NAMESPACE = ""
    settings.IKI_RUNS_VM_OWNER = ""
    with pytest.raises(CommandError, match="IKI_RUNS_NAMESPACE"):
        fleets()
    settings.IKI_RUNS_VM_OWNER = "iki-test"
    settings.IKI_RUNS_VM_SERVER_URL = ""
    settings.IKI_RUNS_WORKER_IMAGE = WORKER_IMAGE
    with pytest.raises(CommandError, match="IKI_RUNS_VM_SERVER_URL"):
        fleets()
    settings.IKI_RUNS_VM_SERVER_URL = "https://iki.internal.example.com"
    (only,) = fleets()
    assert isinstance(only, Ec2Fleet)
