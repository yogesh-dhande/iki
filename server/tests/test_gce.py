"""What the provisioner sends Compute Engine (ADR 0074, 0107), checked against
google-cloud-compute's own types (which refuse a field Compute Engine doesn't have), how
Compute Engine's errors are read, and the script each machine boots with."""

import dataclasses
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from google.api_core import exceptions
from google.cloud import compute_v1

from iki_server.runs import gce
from iki_server.runs.ec2 import VmMachines
from iki_server.runs.gce import (
    GceError,
    GoogleGce,
    image_request,
    instance_request,
    is_capacity,
    is_refused,
    label,
    machine_name,
    public,
    root_disk,
    startup_script,
)
from iki_spec.compute import Template
from iki_spec.model import Resources

GI = 1024**3
IMAGE = "us-central1-docker.pkg.dev/acme-iki/plugins/alphafold@sha256:" + "9" * 64
WORKER = "ghcr.io/yogesh-dhande/iki-worker:0.1.0@sha256:" + "b" * 64
MACHINES = VmMachines(
    server_url="https://iki.internal.example.com", worker_image=WORKER, owner="iki-prod"
)
SOURCE = "projects/acme-iki/regions/us-central1/instanceTemplates/iki-prod-vm-20261116"
KEY = "projects/acme-iki/locations/us-central1/keyRings/iki-prod/cryptoKeys/vm-disks"
COS = "projects/cos-cloud/global/images/cos-stable-121-18867-90-0"
LAUNCH = "01a1d3a1-04c4-7657-86e7-9bdd0aa33e57"


def template(**gcp: Any) -> Template:
    return Template(
        name="a100-spot",
        provisioner="gcp_gce",
        lifecycle="ephemeral",
        capacity=Resources(cpu=96, memory=1360 * GI, gpu_count=8, scratch_disk=500 * GI),
        gcp={
            "machine_type": "a2-highgpu-8g",
            "image": "projects/cos-cloud/global/images/family/cos-stable",
            "spot": True,
            "region": "us-central1",
            "zones": ["us-central1-a", "us-central1-b"],
            "labels": {"cost-center": "rd-42"},
            **gcp,
        },
    )


def request(spot: bool = True, attempt: int = 0, image: str = IMAGE, **gcp: Any) -> dict[str, Any]:
    return instance_request(
        launch_id=LAUNCH,
        name="iki-run-01a1d3a104c4765786e79bdd0aa33e57",
        machine=machine_name("iki-prod", "iki-run-01a1d3a104c4765786e79bdd0aa33e57"),
        template=template(**gcp),
        environment="acme/prediction.alphafold-gpu",
        image=image,
        machines=MACHINES,
        once=True,
        token="iki_wlch_secret",
        zone="us-central1-a",
        spot=spot,
        attempt=attempt,
        source=SOURCE,
        boot_image=COS,
        kms_key=KEY,
    )


def test_the_instance_request_is_one_compute_engine_takes() -> None:
    made = request()
    # The client refuses a field the Instance resource doesn't have.
    instance = compute_v1.Instance.from_json(json.dumps(made["instance"]))
    assert instance.name == "iki-prod-run-01a1d3a104c4765786e79bdd0aa33e57"
    assert len(instance.name) <= 63
    assert instance.machine_type == "zones/us-central1-a/machineTypes/a2-highgpu-8g"
    assert made["sourceInstanceTemplate"] == SOURCE
    # Shielded VM, and no public address (no access config: it's the template's subnet).
    assert made["instance"]["shieldedInstanceConfig"] == {
        "enableSecureBoot": True,
        "enableVtpm": True,
        "enableIntegrityMonitoring": True,
    }
    assert "networkInterfaces" not in made["instance"]
    # Spot, deleted when it's taken back, with two minutes' notice.
    assert made["instance"]["scheduling"] == {
        "provisioningModel": "SPOT",
        "instanceTerminationAction": "DELETE",
        "preemptionNoticeDuration": {"seconds": "120"},
        "onHostMaintenance": "TERMINATE",
        "automaticRestart": False,
    }
    # Its disk: encrypted with the deployment's key, room for scratch and the system, gone
    # with it.
    (boot,) = made["instance"]["disks"]
    assert boot["boot"] and boot["autoDelete"]
    assert boot["diskEncryptionKey"] == {"kmsKeyName": KEY}
    assert boot["initializeParams"]["sourceImage"] == COS
    assert boot["initializeParams"]["diskSizeGb"] == "540"
    assert boot["initializeParams"]["diskType"] == "zones/us-central1-a/diskTypes/pd-balanced"
    # Labels Compute Engine takes, which say whose it is.
    labels = made["instance"]["labels"]
    assert labels == {
        "cost-center": "rd-42",
        "iki-deployment": "iki-prod",
        "iki-template": "a100-spot",
        "iki-environment": "acme-prediction-alphafold-gpu",
        "iki-launch": LAUNCH,
    }
    assert boot["initializeParams"]["labels"] == labels
    # Metadata replaces the template's: so it says again what the template locks down.
    metadata = {i["key"]: i["value"] for i in made["instance"]["metadata"]["items"]}
    assert metadata["block-project-ssh-keys"] == "TRUE"
    assert metadata["enable-oslogin"] == "TRUE"
    assert metadata["serial-port-enable"] == "FALSE"
    assert startup_script(made["instance"]) == metadata["startup-script"]
    compute_v1.InsertInstanceRequest(
        project="acme-iki",
        zone="us-central1-a",
        source_instance_template=made["sourceInstanceTemplate"],
        request_id=made["requestId"],
        instance_resource=instance,
    )


def test_each_try_has_its_own_request_id() -> None:
    assert request()["requestId"] == request()["requestId"]
    assert request(attempt=1)["requestId"] != request()["requestId"]


def test_a_standard_machine_with_gpus_is_stopped_for_maintenance_never_migrated() -> None:
    scheduling = request(spot=False)["instance"]["scheduling"]
    assert scheduling == {
        "provisioningModel": "STANDARD",
        "onHostMaintenance": "TERMINATE",
        "automaticRestart": False,
    }


def test_a_standard_machine_without_gpus_is_migrated_through_maintenance() -> None:
    """E2 machines refuse to be stopped for host maintenance unless Spot, so they're migrated
    live: the run goes on (found by the live test, on an e2-small)."""
    made = instance_request(
        launch_id=LAUNCH,
        name="iki-run-1",
        machine="iki-prod-run-1",
        template=dataclasses.replace(
            template(machine_type="e2-small", spot=False),
            capacity=Resources(cpu=0.5, memory=GI, scratch_disk=2 * GI),
        ),
        environment="acme/prediction.alphafold-gpu",
        image=IMAGE,
        machines=MACHINES,
        once=True,
        token="iki_wlch_secret",
        zone="us-central1-a",
        spot=False,
        attempt=0,
        source=SOURCE,
        boot_image=COS,
        kms_key=KEY,
    )
    assert made["instance"]["scheduling"] == {
        "provisioningModel": "STANDARD",
        "onHostMaintenance": "MIGRATE",
        "automaticRestart": False,
    }
    assert "guestAccelerators" not in made["instance"]


def test_gpus_are_attached_on_machine_types_without_their_own() -> None:
    made = request(machine_type="n1-standard-16", gpu_type="nvidia-tesla-t4")["instance"]
    assert made["guestAccelerators"] == [
        {
            "acceleratorType": "zones/us-central1-a/acceleratorTypes/nvidia-tesla-t4",
            "acceleratorCount": 8,
        }
    ]
    compute_v1.Instance.from_json(json.dumps(made))
    assert "guestAccelerators" not in request()["instance"]


def test_the_disk_holds_the_scratch_space_and_the_system() -> None:
    assert root_disk(template()) == 540
    assert root_disk(template(root_disk="1Ti")) == 1024
    small = request(root_disk="1Gi")["instance"]["disks"][0]["initializeParams"]
    assert small["diskSizeGb"] == "10"  # Compute Engine's least


def test_names_and_labels_are_ones_compute_engine_takes() -> None:
    assert machine_name("iki-prod", "iki-setup-" + "a" * 32) == "iki-prod-setup-" + "a" * 32
    assert label("acme/prediction.AlphaFold-GPU") == "acme-prediction-alphafold-gpu"
    assert len(label("x" * 100)) == 63


def test_an_instance_template_with_a_public_address_is_refused() -> None:
    assert not public({"networkInterfaces": [{"subnetwork": "s"}]})
    assert public({"networkInterfaces": [{"accessConfigs": [{"type": "ONE_TO_ONE_NAT"}]}]})


def test_errors_say_whether_to_wait_or_give_up() -> None:
    cases = [
        (GceError("ZONE_RESOURCE_POOL_EXHAUSTED", "Try a different zone."), (True, False)),
        (GceError("QUOTA_EXCEEDED", "Quota 'NVIDIA_A100_GPUS' exceeded."), (True, False)),
        (
            GceError(
                "notFound", "The resource 'zones/us-central1-f/machineTypes/a2' was not found"
            ),
            (True, False),
        ),
        (GceError("forbidden", "Required 'compute.instances.create' permission"), (False, True)),
        (
            GceError("notFound", "The resource 'projects/x/global/images/y' was not found"),
            (False, True),
        ),
        (GceError("SERVICE_ACCOUNT_ACCESS_DENIED", "Can't act as it."), (False, True)),
        (GceError("unreachable", "Timed out."), (False, False)),  # tried again soon
    ]
    assert [(is_capacity(e), is_refused(e)) for e, _ in cases] == [want for _, want in cases]


def _operation_error(code: str) -> exceptions.GoogleAPICallError:
    return exceptions.from_http_status(  # type: ignore[no-any-return,no-untyped-call]
        400,
        f"{code}: it failed",
        errors=[compute_v1.Errors(code=code, message="it failed")],
    )


class _Operation:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def result(self, timeout: float) -> None:
        if self.error is not None:
            raise self.error


class _Client:
    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        def call(**params: Any) -> Any:
            self.calls.append((name, params))
            answer = self.answers[name]
            if isinstance(answer, Exception):
                raise answer
            return answer(**params) if callable(answer) else answer

        return call


def google(**clients: _Client) -> GoogleGce:
    made = GoogleGce()
    made._clients.update(clients)
    return made


def test_an_insert_waits_for_its_operation_and_says_what_went_wrong() -> None:
    instances = _Client(insert=_Operation(_operation_error("ZONE_RESOURCE_POOL_EXHAUSTED")))
    with pytest.raises(GceError) as caught:
        google(InstancesClient=instances).insert("acme-iki", "us-central1-a", request())
    assert caught.value.code == "ZONE_RESOURCE_POOL_EXHAUSTED"
    assert is_capacity(caught.value)
    ((_, params),) = instances.calls
    sent = params["request"]
    assert (sent.project, sent.zone, sent.source_instance_template) == (
        "acme-iki",
        "us-central1-a",
        SOURCE,
    )
    assert sent.instance_resource.scheduling.provisioning_model == "SPOT"


def test_an_http_error_is_read_by_its_reason() -> None:
    refused = exceptions.Forbidden(  # type: ignore[no-untyped-call]
        "Required 'compute.instances.create' permission",
        errors=[{"reason": "forbidden", "message": "Required ... permission"}],
    )
    with pytest.raises(GceError) as caught:
        google(InstancesClient=_Client(insert=refused)).insert("p", "z", request())
    assert (caught.value.code, is_refused(caught.value)) == ("forbidden", True)


def test_an_instance_made_already_is_started() -> None:
    made = exceptions.Conflict(  # type: ignore[no-untyped-call]
        "exists", errors=[{"reason": "alreadyExists"}]
    )
    google(InstancesClient=_Client(insert=_Operation(made))).insert("p", "z", request())


def test_it_lists_the_deployments_instances_in_every_zone() -> None:
    def listed(request: Any) -> Any:
        assert request.filter == 'labels.iki-deployment = "iki-prod"'
        scoped = compute_v1.InstancesScopedList(
            instances=[
                compute_v1.Instance(
                    name="iki-prod-run-1",
                    status="RUNNING",
                    zone="https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-a",
                    labels={"iki-launch": "1"},
                )
            ]
        )
        return iter(
            [
                ("zones/us-central1-a", scoped),
                ("zones/us-east1-b", compute_v1.InstancesScopedList()),
            ]
        )

    found = google(InstancesClient=_Client(aggregated_list=listed)).instances("p", "iki-prod")
    assert [(i["name"], i["zone"], i["status"], i["labels"]) for i in found] == [
        ("iki-prod-run-1", "us-central1-a", "RUNNING", {"iki-launch": "1"})
    ]


def test_it_reads_the_serial_port_operations_templates_and_images() -> None:
    serial = create_autospec(compute_v1.InstancesClient, instance=True).get_serial_port_output
    serial.return_value = compute_v1.SerialPortOutput(contents="IKI-HOST-ERROR x: y\n")
    instances = _Client(
        get_serial_port_output=serial,
        delete=exceptions.NotFound(  # type: ignore[no-untyped-call]
            "gone", errors=[{"reason": "notFound"}]
        ),
    )
    operations = _Client(
        list=lambda request: iter(
            [compute_v1.Operation(operation_type="compute.instances.preempted")]
        )
    )
    templates = _Client(
        get=compute_v1.InstanceTemplate(
            properties=compute_v1.InstanceProperties(
                network_interfaces=[compute_v1.NetworkInterface(subnetwork="s")]
            )
        )
    )
    images = _Client(get_from_family=compute_v1.Image(self_link=f"https://x/{COS}"))
    client = google(
        InstancesClient=instances,
        ZoneOperationsClient=operations,
        RegionInstanceTemplatesClient=templates,
        ImagesClient=images,
    )
    assert client.console("p", "z", "n") == "IKI-HOST-ERROR x: y\n"
    serial.assert_called_once_with(
        request=compute_v1.GetSerialPortOutputInstanceRequest(
            project="p", zone="z", instance="n", port=1
        )
    )
    client.delete("p", "z", "n")  # already gone: nothing
    assert client.history("p", "us-central1-a", "n") == ["compute.instances.preempted"]
    properties = client.template("p", SOURCE)
    assert properties["networkInterfaces"][0]["subnetwork"] == "s"
    assert not public(properties)
    assert templates.calls[0][1] == {
        "project": "acme-iki",
        "region": "us-central1",
        "instance_template": "iki-prod-vm-20261116",
    }
    assert client.image("p", "projects/cos-cloud/global/images/family/cos-stable") == (
        f"https://x/{COS}"
    )
    assert images.calls[0][1] == {"project": "cos-cloud", "family": "cos-stable"}
    assert client.image("p", COS) == COS


def made_image() -> dict[str, Any]:
    return image_request(
        build_id="0193a1b2-0000-7000-8000-000000000001",
        name="iki-prod-setup-000001-v1",
        description="acme/prediction.alphafold-gpu af-db, version 1 (SETUP-000001)",
        disk="projects/acme-iki/zones/us-central1-a/disks/iki-prod-setup-000001-d1",
        region="us-central1",
        kms_key=KEY,
        machines=MACHINES,
        environment="acme/prediction.alphafold-gpu",
        volume="af-db",
    )


def test_a_setup_builds_disk_is_made_into_an_encrypted_image_in_its_region() -> None:
    made = made_image()
    image = compute_v1.Image.from_json(json.dumps(made["image"]))
    assert image.source_disk.endswith("/disks/iki-prod-setup-000001-d1")
    assert list(image.storage_locations) == ["us-central1"]
    assert image.image_encryption_key.kms_key_name == KEY
    assert dict(image.labels) == {
        "iki-deployment": "iki-prod",
        "iki-setup": "0193a1b2-0000-7000-8000-000000000001",
        "iki-volume": "af-db",
        "iki-environment": "acme-prediction-alphafold-gpu",
    }
    # One image however often it's asked for.
    assert made["requestId"] == made_image()["requestId"]


def test_it_makes_lists_and_deletes_disks_and_images() -> None:
    def disks_listed(request: Any) -> Any:
        assert request.filter == 'labels.iki-deployment = "iki-prod"'
        scoped = compute_v1.DisksScopedList(
            disks=[
                compute_v1.Disk(
                    name="iki-prod-setup-000001-d1",
                    status="READY",
                    zone="https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-a",
                    labels={"iki-setup": "1"},
                )
            ]
        )
        return iter([("zones/us-central1-a", scoped)])

    gone = exceptions.NotFound(  # type: ignore[no-untyped-call]
        "gone", errors=[{"reason": "notFound"}]
    )
    disks = _Client(aggregated_list=disks_listed, delete=gone)
    images = _Client(
        insert=exceptions.Conflict(  # type: ignore[no-untyped-call]
            "exists", errors=[{"reason": "alreadyExists"}]
        ),
        list=lambda request: iter(
            [compute_v1.Image(name="iki-prod-setup-000001-v1", status="PENDING")]
        ),
        delete=gone,
    )
    client = google(DisksClient=disks, ImagesClient=images)
    (disk,) = client.disks("p", "iki-prod")
    assert (disk["name"], disk["zone"], disk["status"]) == (
        "iki-prod-setup-000001-d1",
        "us-central1-a",
        "READY",
    )
    assert not disk.get("users")  # detached
    client.delete_disk("p", "us-central1-a", "iki-prod-setup-000001-d1")  # gone already
    client.create_image("p", made_image())  # made already
    sent = images.calls[0][1]["request"]
    assert (sent.project, sent.request_id) == ("p", made_image()["requestId"])
    assert sent.image_resource.name == "iki-prod-setup-000001-v1"
    assert [i["status"] for i in client.images("p", "iki-prod")] == ["PENDING"]
    assert images.calls[1][1]["request"].filter == 'labels.iki-deployment = "iki-prod"'
    client.delete_image("p", "iki-prod-setup-000001-v1")  # gone already


def test_no_credentials_is_refused() -> None:
    from google.auth import exceptions as auth

    missing = auth.DefaultCredentialsError("none")  # type: ignore[no-untyped-call]
    with pytest.raises(GceError) as caught:
        google(InstancesClient=_Client(delete=missing)).delete("p", "z", "n")
    assert (caught.value.code, is_refused(caught.value)) == ("no_credentials", True)


def test_the_script_is_valid_shell_and_signs_in_to_artifact_registry(tmp_path: Path) -> None:
    text = startup_script(request()["instance"])
    file = tmp_path / "startup-script.sh"
    file.write_text(text)
    checked = subprocess.run(  # noqa: S603 - our own script
        ["/bin/sh", "-n", str(file)], capture_output=True, text=True, check=False
    )
    assert checked.returncode == 0, checked.stderr
    shellcheck = shutil.which("shellcheck")
    if shellcheck:
        linted = subprocess.run(  # noqa: S603 - our own script
            [shellcheck, "-s", "sh", str(file)], capture_output=True, text=True, check=False
        )
        assert linted.returncode == 0, linted.stdout
    assert f"docker pull {WORKER}" in text
    # Kept where Container-Optimized OS lets programs run.
    assert "mount -o remount,bind,exec,nosuid,nodev /var/lib/iki" in text
    assert "/var/lib/iki/host/bin/iki-worker host /etc/iki/host.json" in text
    assert "Environment=DOCKER_CONFIG=/var/lib/iki/docker" in text
    # Its image in Artifact Registry: Docker signs in with the machine's own token.
    assert "instance/service-accounts/default/token" in text
    assert "--password-stdin https://us-central1-docker.pkg.dev" in text
    assert "ecs" not in text
    config = json.loads(text.split("IKI_HOST_JSON'\n")[1].split("\nIKI_HOST_JSON")[0])
    assert "iki_wlch_secret" not in json.dumps(config)
    # The host watches Compute Engine's preemption notice, not EC2's.
    assert (config["cloud"], config["spot"]) == ("gcp", True)
    assert config["worker"] == "/var/lib/iki/host/bin/iki-worker"
    assert config["scratch"] == "/var/lib/iki/scratch"


def test_a_runs_container_may_ask_compute_engines_dns_and_nothing_else_there() -> None:
    lines = startup_script(request()["instance"]).splitlines()
    drop = lines.index(
        "iptables -I DOCKER-USER -d 169.254.169.254/32 -j DROP >/dev/null 2>&1 || true"
    )
    allowed = [
        line for line in lines if "169.254.169.254/32" in line and "--dport 53 -j ACCEPT" in line
    ]
    assert [line.split(" -p ")[1].split()[0] for line in allowed] == ["udp", "tcp"]
    # Each is inserted at the top of DOCKER-USER after the drop, so each comes before it.
    assert all(lines.index(line) > drop for line in allowed)
    assert not [
        line
        for line in lines
        if "169.254.169.254" in line and "ACCEPT" in line and "53" not in line
    ]


def test_ec2s_machines_need_no_such_exception() -> None:
    from iki_server.runs import ec2

    made = ec2.user_data(
        name="iki-run-1",
        template=template(),
        environment="acme/prediction.alphafold-gpu",
        image=IMAGE,
        machines=MACHINES,
        once=True,
        spot=False,
        token="iki_wlch_secret",
    )
    assert "--dport 53" not in made


def test_the_deployments_ca_reaches_the_host_when_it_has_one() -> None:
    def config(machines: VmMachines) -> dict[str, Any]:
        made = instance_request(
            launch_id=LAUNCH,
            name="iki-run-1",
            machine="iki-prod-run-1",
            template=template(),
            environment="acme/prediction.alphafold-gpu",
            image=IMAGE,
            machines=machines,
            once=True,
            token="iki_wlch_secret",
            zone="us-central1-a",
            spot=False,
            attempt=0,
            source=SOURCE,
            boot_image=COS,
            kms_key=KEY,
        )
        text = startup_script(made["instance"])
        found: dict[str, Any] = json.loads(
            text.split("IKI_HOST_JSON'\n")[1].split("\nIKI_HOST_JSON")[0]
        )
        return found

    assert "ca_certificates" not in config(MACHINES)
    pem = "-----BEGIN CERTIFICATE-----\nMIIBdeployment\n-----END CERTIFICATE-----\n"
    trusting = VmMachines(
        server_url=MACHINES.server_url,
        worker_image=WORKER,
        owner="iki-prod",
        ca_certificates=pem,
    )
    assert config(trusting)["ca_certificates"] == pem


def test_a_public_registry_needs_no_sign_in() -> None:
    made = request(image="ghcr.io/acme/alphafold@sha256:" + "9" * 64)
    assert "docker login" not in startup_script(made["instance"])


def test_ec2s_script_is_unchanged_by_sharing_it() -> None:
    from iki_server.runs import ec2

    assert ec2.AWS.root == "/opt/iki"
    assert gce.GCE.root == "/var/lib/iki/host"
