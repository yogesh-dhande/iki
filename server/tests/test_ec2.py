"""What the provisioner sends EC2 (ADR 0074), checked against EC2's own API model by
botocore's Stubber, and the script each run machine boots with."""

import base64
import dataclasses
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from iki_server.runs.ec2 import (
    Boto3Ec2,
    Disk,
    Ec2Error,
    VmMachines,
    host_trouble,
    instance_request,
    is_capacity,
    is_refused,
    root_disk,
    user_data,
)
from iki_spec.compute import Template, TemplateVolume
from iki_spec.model import Resources

GI = 1024**3
IMAGE = "123456789012.dkr.ecr.us-east-1.amazonaws.com/alphafold@sha256:" + "9" * 64
WORKER = "ghcr.io/yogesh-dhande/iki-worker:0.1.0@sha256:" + "b" * 64
MACHINES = VmMachines(
    server_url="https://iki.internal.example.com", worker_image=WORKER, owner="iki"
)


def template(**aws: Any) -> Template:
    return Template(
        name="a100-spot",
        provisioner="aws_ec2",
        lifecycle="ephemeral",
        capacity=Resources(cpu=96, memory=1152 * GI, gpu_count=8, scratch_disk=500 * GI),
        aws={
            "instance_type": "p4d.24xlarge",
            "image": "ami-0123456789abcdef0",
            "spot": True,
            "region": "us-east-1",
            "subnets": ["subnet-0a1b2c3d4e5f60718"],
            "security_groups": ["sg-0a1b2c3d4e5f60718"],
            "instance_profile": "iki-vm-worker",
            **aws,
        },
    )


def request(**aws: Any) -> dict[str, Any]:
    return instance_request(
        launch_id="01a1d3a1-04c4-7657-86e7-9bdd0aa33e57",
        name="iki-run-01a1d3a104c4765786e79bdd0aa33e57",
        template=template(**aws),
        environment="structure_prediction.alphafold-gpu",
        image=IMAGE,
        machines=MACHINES,
        once=True,
        token="iki_wlch_secret",
        subnet="subnet-0a1b2c3d4e5f60718",
        spot=True,
        attempt=0,
    )


def stubbed() -> tuple[Boto3Ec2, Stubber]:
    client = boto3.session.Session(
        aws_access_key_id="AKIAEXAMPLE", aws_secret_access_key="example", region_name="us-east-1"
    ).client("ec2")
    ec2 = Boto3Ec2()
    ec2._clients["us-east-1"] = client
    return ec2, Stubber(client)


INSTANCE = {"InstanceId": "i-0123456789abcdef0", "State": {"Name": "pending", "Code": 0}}


def test_the_instance_request_is_one_ec2_takes() -> None:
    ec2, stub = stubbed()
    sent = request(kms_key="alias/iki-runs", tags={"cost-center": "rd-42"})
    # botocore checks every parameter against EC2's model before the stub answers.
    stub.add_response("run_instances", {"Instances": [INSTANCE]}, None)
    with stub:
        assert ec2.run_instance("us-east-1", sent)["InstanceId"] == INSTANCE["InstanceId"]
    stub.assert_no_pending_responses()
    assert len(sent["ClientToken"]) <= 64
    assert len(sent["UserData"].encode()) < 16 * 1024  # EC2's limit


def test_a_machine_with_volumes_gets_a_disk_for_each_that_ec2_takes() -> None:
    ec2, stub = stubbed()
    disks = dataclasses.replace(
        template(kms_key="alias/iki-runs"),
        volumes={"af-db": TemplateVolume("clone_from_snapshot", True, iops=16000)},
    )
    common: dict[str, Any] = {
        "launch_id": "01a1d3a1-04c4-7657-86e7-9bdd0aa33e57",
        "name": "iki-setup-01a1d3a104c4765786e79bdd0aa33e57",
        "template": disks,
        "environment": "structure_prediction.alphafold-gpu",
        "image": IMAGE,
        "machines": MACHINES,
        "once": True,
        "token": "iki_wlch_secret",
        "subnet": "subnet-0a1b2c3d4e5f60718",
        "attempt": 0,
    }
    setup = instance_request(
        **common,
        spot=False,
        data=[Disk("af-db", "/data/af", 3 * 1024 * GI), Disk("pdb", "/data/pdb", 300 * GI)],
        setup="01a1d3a1-04c4-7657-86e7-9bdd0aa33e58",
    )
    run = instance_request(
        **common,
        spot=True,
        data=[Disk("af-db", "/data/af", 3 * 1024 * GI, "snap-0123456789abcdef0")],
    )
    for _ in (setup, run):
        stub.add_response("run_instances", {"Instances": [INSTANCE]}, None)
    with stub:
        for each in (setup, run):
            ec2.run_instance("us-east-1", each)
    stub.assert_no_pending_responses()
    assert [m["DeviceName"] for m in setup["BlockDeviceMappings"]] == [
        "/dev/xvda",
        "/dev/sdf",
        "/dev/sdg",
    ]
    assert setup["BlockDeviceMappings"][2]["Ebs"] == {
        "VolumeSize": 300,
        "VolumeType": "gp3",
        "Encrypted": True,
        "DeleteOnTermination": False,
        "KmsKeyId": "alias/iki-runs",
    }
    assert run["BlockDeviceMappings"][1]["Ebs"]["SnapshotId"] == "snap-0123456789abcdef0"
    assert run["BlockDeviceMappings"][1]["Ebs"]["DeleteOnTermination"] is True
    assert len(setup["UserData"].encode()) < 16 * 1024


def test_it_snapshots_disks_and_makes_them_fast_to_restore_as_ec2_takes() -> None:
    ec2, stub = stubbed()
    ours = [
        {"Name": "tag:iki.ikisci.org/deployment", "Values": ["iki"]},
        {"Name": "tag-key", "Values": ["iki.ikisci.org/setup"]},
    ]
    disk = {"VolumeId": "vol-0123456789abcdef0", "State": "available"}
    snapshot = {"SnapshotId": "snap-0123456789abcdef0", "State": "pending"}
    stub.add_response("describe_volumes", {"Volumes": [disk]}, {"Filters": ours, "MaxResults": 500})
    stub.add_response(
        "create_snapshot",
        snapshot,
        {
            "VolumeId": disk["VolumeId"],
            "Description": "af-db, version 2",
            "TagSpecifications": [
                {
                    "ResourceType": "snapshot",
                    "Tags": [{"Key": "iki.ikisci.org/setup", "Value": "b"}],
                }
            ],
        },
    )
    stub.add_response(
        "describe_snapshots",
        {"Snapshots": [snapshot]},
        {"OwnerIds": ["self"], "Filters": ours, "MaxResults": 1000},
    )
    stub.add_response(
        "describe_subnets",
        {"Subnets": [{"SubnetId": "subnet-0a1b2c3d4e5f60718", "AvailabilityZone": "us-east-1a"}]},
        {"SubnetIds": ["subnet-0a1b2c3d4e5f60718"]},
    )
    stub.add_response(
        "enable_fast_snapshot_restores",
        {
            "Unsuccessful": [
                {
                    "SnapshotId": snapshot["SnapshotId"],
                    "FastSnapshotRestoreStateErrors": [
                        {
                            "AvailabilityZone": "us-east-1a",
                            "Error": {"Code": "LimitExceeded", "Message": "Too many."},
                        }
                    ],
                }
            ]
        },
        {"AvailabilityZones": ["us-east-1a"], "SourceSnapshotIds": [snapshot["SnapshotId"]]},
    )
    stub.add_response(
        "describe_fast_snapshot_restores",
        {
            "FastSnapshotRestores": [
                {
                    "SnapshotId": snapshot["SnapshotId"],
                    "AvailabilityZone": "us-east-1a",
                    "State": "optimizing",
                }
            ]
        },
        {
            "Filters": [{"Name": "snapshot-id", "Values": [snapshot["SnapshotId"]]}],
            "MaxResults": 200,
        },
    )
    stub.add_response(
        "disable_fast_snapshot_restores",
        {},
        {"AvailabilityZones": ["us-east-1a"], "SourceSnapshotIds": [snapshot["SnapshotId"]]},
    )
    stub.add_client_error("delete_snapshot", "InvalidSnapshot.NotFound", "Gone.")
    stub.add_client_error("delete_volume", "InvalidVolume.NotFound", "Gone.")
    with stub:
        assert ec2.volumes("us-east-1", "iki") == [disk]
        made = ec2.create_snapshot(
            "us-east-1", disk["VolumeId"], "af-db, version 2", {"iki.ikisci.org/setup": "b"}
        )
        assert made["SnapshotId"] == snapshot["SnapshotId"]
        assert ec2.snapshots("us-east-1", "iki") == [snapshot]
        assert ec2.zones("us-east-1", ["subnet-0a1b2c3d4e5f60718"]) == {
            "subnet-0a1b2c3d4e5f60718": "us-east-1a"
        }
        refused = ec2.enable_fast_restores("us-east-1", snapshot["SnapshotId"], ["us-east-1a"])
        assert refused == ["LimitExceeded: Too many."]
        (state,) = ec2.fast_restores("us-east-1", [snapshot["SnapshotId"]])
        assert state["State"] == "optimizing"
        assert ec2.fast_restores("us-east-1", []) == []
        ec2.disable_fast_restores("us-east-1", snapshot["SnapshotId"], ["us-east-1a"])
        ec2.delete_snapshot("us-east-1", snapshot["SnapshotId"])
        ec2.delete_volume("us-east-1", disk["VolumeId"])
    stub.assert_no_pending_responses()


def test_the_disk_holds_the_scratch_space_and_the_system() -> None:
    assert root_disk(template()) == 540
    assert root_disk(template(root_disk="1Ti")) == 1024


def test_errors_say_whether_to_wait_or_give_up() -> None:
    ec2, stub = stubbed()
    stub.add_client_error(
        "run_instances", "InsufficientInstanceCapacity", "We currently do not have capacity."
    )
    stub.add_client_error("run_instances", "UnauthorizedOperation", "You are not authorized.")
    stub.add_client_error("run_instances", "RequestLimitExceeded", "Slow down.")
    with stub:
        errors = []
        for _ in range(3):
            with pytest.raises(Ec2Error) as caught:
                ec2.run_instance("us-east-1", request())
            errors.append(caught.value)
    assert [(is_capacity(e), is_refused(e)) for e in errors] == [
        (True, False),
        (False, True),
        (False, False),  # it's retried soon, not given up on
    ]


def test_it_lists_the_deployments_instances_page_by_page() -> None:
    ec2, stub = stubbed()
    owner = [{"Name": "tag:iki.ikisci.org/deployment", "Values": ["iki"]}]
    stub.add_response(
        "describe_instances",
        {"Reservations": [{"Instances": [INSTANCE]}], "NextToken": "more"},
        {"Filters": owner, "MaxResults": 1000},
    )
    stub.add_response(
        "describe_instances",
        {"Reservations": [{"Instances": [{**INSTANCE, "InstanceId": "i-0fedcba9876543210"}]}]},
        {"Filters": owner, "MaxResults": 1000, "NextToken": "more"},
    )
    with stub:
        found = ec2.instances("us-east-1", "iki")
    assert [i["InstanceId"] for i in found] == ["i-0123456789abcdef0", "i-0fedcba9876543210"]


def test_terminating_one_already_gone_is_nothing() -> None:
    ec2, stub = stubbed()
    stub.add_client_error("terminate_instances", "InvalidInstanceID.NotFound", "Gone.")
    with stub:
        ec2.terminate("us-east-1", ["i-0123456789abcdef0"])


def test_it_reads_why_a_host_gave_up_from_its_console() -> None:
    ec2, stub = stubbed()
    said = "booting\nIKI-HOST-ERROR image_unavailable: couldn't pull it: denied\nshutting down\n"
    stub.add_response(
        "get_console_output",
        {"InstanceId": "i-0123456789abcdef0", "Output": base64.b64encode(said.encode()).decode()},
        {"InstanceId": "i-0123456789abcdef0", "Latest": True},
    )
    with stub:
        console = ec2.console("us-east-1", "i-0123456789abcdef0")
    assert host_trouble(console) == ("image_unavailable", "couldn't pull it: denied")
    assert host_trouble("all fine\n") is None


# --- The machine's script --------------------------------------------------------------------


def script(**overrides: Any) -> str:
    return user_data(
        name="iki-run-1",
        template=template(),
        environment="structure_prediction.alphafold-gpu",
        image=overrides.get("image", IMAGE),
        machines=MACHINES,
        once=True,
        spot=True,
        token="iki_wlch_secret",
    )


def test_the_script_is_valid_shell_and_gets_the_worker_from_its_pinned_image(
    tmp_path: Path,
) -> None:
    text = script()
    file = tmp_path / "user-data.sh"
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
    assert "install /out" in text
    assert "iki-worker host /etc/iki/host.json" in text
    # Its images in ECR: Docker signs in with the machine's own role.
    assert "aws ecr get-login-password --region us-east-1" in text
    assert "123456789012.dkr.ecr.us-east-1.amazonaws.com" in text
    # The token goes in a file of its own, never in the host's config.
    config = text.split("IKI_HOST_JSON'\n")[1].split("\nIKI_HOST_JSON")[0]
    assert "iki_wlch_secret" not in config
    assert json.loads(config)["image"] == IMAGE


def test_a_public_registry_needs_no_sign_in() -> None:
    assert "docker login" not in script(image="ghcr.io/acme/alphafold@sha256:" + "9" * 64)
