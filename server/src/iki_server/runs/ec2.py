"""What the provisioner asks EC2 for (ADR 0074), and how.

:class:`Ec2` is the little of the EC2 API the provisioner uses; :class:`Boto3Ec2` calls it
with the provisioner's own AWS credentials (from its pod's IAM role, by the default chain).

:func:`instance_request` is a run machine's ``RunInstances`` request. The instance boots
the template's image, in a private subnet with no public address and the template's
security groups, with:

- IMDSv2 only, and a hop limit of one, so only the machine's own processes reach its
  metadata (its user data, its role's credentials): not a run's container, one hop further.
- An encrypted disk, deleted with it.
- Tags naming the deployment, the launch, its template and environment, so the
  provisioner finds its own instances and nobody else's, and an IAM policy can say so.
- ``terminate`` when it shuts itself down: an ephemeral machine does once its run is done.

Its user data (:func:`user_data`) copies ``iki-worker`` out of Iki's signed worker image,
pinned by its digest, and starts ``iki-worker host``. That pulls the environment's image,
pinned by its digest too, and runs it with the worker in it, as the container tier does:
the worker registers with the launch's token and runs one job, or one after another.
When AWS says it will take a spot instance back, the host stops the worker, which saves a
checkpointable run's progress (ADR 0069) and says the attempt was interrupted.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
from dataclasses import dataclass
from typing import Any, Protocol

from iki_server.runs.machines import BIN, SCRATCH, UID, worker_settings
from iki_spec.compute import ROOT_DEFAULT, Template
from iki_spec.model import parse_quantity

__all__ = [
    "LAUNCH_TAG",
    "OWNER_TAG",
    "Boto3Ec2",
    "Ec2",
    "Ec2Error",
    "VmMachines",
    "host_config",
    "host_trouble",
    "instance_request",
    "is_capacity",
    "is_refused",
    "user_data",
]

#: Every instance the provisioner starts carries these tags: whose deployment it is, and
#: its launch's ID.
OWNER_TAG = "iki.ikisci.org/deployment"
LAUNCH_TAG = "iki.ikisci.org/launch"
TEMPLATE_TAG = "iki.ikisci.org/template"
ENVIRONMENT_TAG = "iki.ikisci.org/environment"

#: Where things live on a run machine.
HOST = "/opt/iki"
HOST_WORKER = f"{HOST}/bin/iki-worker"
HOST_SCRATCH = "/var/lib/iki/scratch"
CONFIG = "/etc/iki/host.json"
TOKEN = "/etc/iki/token"  # noqa: S105 - where it's kept, not a token
#: Seconds the worker gets to stop when AWS takes a spot instance back: it warns two
#: minutes ahead, and the rest is for stopping the container and shutting down.
SPOT_STOP = 100
#: And when the machine is stopped otherwise (its worker is idle then, or it's being
#: taken away regardless).
STOP = 300

#: What EC2 says when it has no machine to give now: try another zone, the next market, or
#: later. Account limits are here too: they lift when other instances stop.
CAPACITY = frozenset(
    {
        "InsufficientInstanceCapacity",
        "InsufficientHostCapacity",
        "InsufficientCapacity",
        "InsufficientReservedInstanceCapacity",
        "InsufficientFreeAddressesInSubnet",
        "SpotMaxPriceTooLow",
        "MaxSpotInstanceCountExceeded",
        "InstanceLimitExceeded",
        "VcpuLimitExceeded",
        "Unsupported",
    }
)
#: And what means it will refuse again until someone changes something: a policy, an AMI,
#: a subnet or a security group.
REFUSED_PREFIXES = ("Unauthorized", "Invalid", "AuthFailure", "OptInRequired", "Blocked")
REFUSED = frozenset({"no_credentials", "MissingParameter", "UnknownParameter"})

_ECR = re.compile(r"^(?P<host>[0-9]{12}\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com(\.cn)?)/")
_TROUBLE = re.compile(r"IKI-HOST-ERROR (?P<reason>[a-z_]+): (?P<message>.+)")


class Ec2Error(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"EC2 said {code}: {message}")
        self.code = code
        self.message = message


def is_capacity(error: Ec2Error) -> bool:
    return error.code in CAPACITY


def is_refused(error: Ec2Error) -> bool:
    return error.code in REFUSED or error.code.startswith(REFUSED_PREFIXES)


class Ec2(Protocol):
    def run_instance(self, region: str, request: dict[str, Any]) -> dict[str, Any]:
        """Starts one instance as `request` asks; returns it, as EC2 describes it."""

    def instances(self, region: str, owner: str) -> list[dict[str, Any]]:
        """The deployment's instances in `region`, as EC2 describes them, ended ones too
        (for a while)."""

    def terminate(self, region: str, instance_ids: list[str]) -> None:
        """Terminates the instances; nothing for ones already gone."""

    def tag(self, region: str, instance_id: str, tags: dict[str, str], remove: list[str]) -> None:
        """Sets the instance's `tags`, and removes those in `remove`."""

    def console(self, region: str, instance_id: str) -> str:
        """What the instance last wrote to its serial console, if EC2 has it."""


@dataclass(frozen=True)
class VmMachines:
    """What every run machine gets, from the deployment's settings."""

    #: Where machines reach the server: an address in the VPC, never the cluster's own.
    server_url: str
    worker_image: str
    #: This deployment's name in its instances' tags.
    owner: str
    allow_http: bool = False


def root_disk(template: Template) -> int:
    """The instance's disk, in GiB: what the template says, or its scratch space and room
    for the system and the run's image."""
    wanted = template.aws.get("root_disk")
    size = (
        parse_quantity(wanted)
        if wanted is not None
        else (template.capacity.scratch_disk or 0) + ROOT_DEFAULT
    )
    return max(1, -(-int(size or 0) // 1024**3))


def host_config(
    *,
    name: str,
    template: Template,
    environment: str,
    image: str,
    machines: VmMachines,
    once: bool,
    spot: bool,
) -> dict[str, Any]:
    """What ``iki-worker host`` reads: the run's container, and how to stop it."""
    have = template.capacity
    env = worker_settings(
        template,
        environment,
        image,
        server_url=machines.server_url,
        allow_http=machines.allow_http,
        once=once,
    )
    env["IKI_WORKER_NAME"] = name
    return {
        "name": name,
        "image": image,
        "once": once,
        "spot": spot,
        "env": env,
        "worker": HOST_WORKER,
        "worker_path": f"{BIN}/iki-worker",
        "token_file": TOKEN,
        "scratch": HOST_SCRATCH,
        "scratch_path": SCRATCH,
        "user": f"{UID}:{UID}",
        "cpus": have.cpu,
        "memory": have.memory,
        "gpus": have.gpu_count,
        "stop_timeout": STOP,
        "spot_stop_timeout": SPOT_STOP,
    }


_SCRIPT = """\
#!/bin/sh
# An Iki run machine (ADR 0074): {name}, for {environment} on {template}.
# It copies iki-worker out of Iki's worker image, pinned by its digest, and starts
# `iki-worker host`, which runs the environment's image with the worker in it.
set -eu
umask 077
fail() {{
    echo "IKI-HOST-ERROR $1: $2" | tee /dev/console >&2 || true
    shutdown -h now
    exit 1
}}
mkdir -p /etc/iki {host}/bin {scratch}
cat > {config} <<'IKI_HOST_JSON'
{config_json}
IKI_HOST_JSON
cat > {token_file} <<'IKI_HOST_TOKEN'
{token}
IKI_HOST_TOKEN
# AWS's ECS-optimized images start the ECS agent; Iki's machines don't use it.
systemctl disable --now ecs >/dev/null 2>&1 || true
command -v docker >/dev/null 2>&1 || fail unsupported "the machine image has no Docker"
systemctl start docker >/dev/null 2>&1 || true
# Only the machine reaches its metadata: IMDSv2 with one hop does that already.
iptables -I DOCKER-USER -d 169.254.169.254/32 -j DROP >/dev/null 2>&1 || true
{logins}n=0
until docker pull {worker_image}; do
    n=$((n + 1))
    [ "$n" -lt 5 ] || fail worker_unavailable "couldn't pull Iki's worker image "{worker_image}
    sleep $((n * 10))
done
docker run --rm --user 0:0 --network none --entrypoint iki-worker \\
    -v {host}/bin:/out {worker_image} install /out >/dev/null \\
    || fail worker_unavailable "iki-worker couldn't be copied out of its image"
chown {uid}:{uid} {scratch}
cat > /etc/systemd/system/iki-host.service <<'IKI_HOST_UNIT'
[Unit]
Description=Iki run machine
Requires=docker.service
After=docker.service network-online.target

[Service]
ExecStart={host}/bin/iki-worker host {config}
TimeoutStopSec={stop_within}
KillMode=mixed
Restart=no
StandardOutput=journal+console
StandardError=journal+console
IKI_HOST_UNIT
systemctl daemon-reload
systemctl start --no-block iki-host.service
"""


def _logins(*images: str) -> str:
    """Signs Docker in to each ECR registry the images are in, with the machine's role (the
    instance profile needs only to read them)."""
    done: set[str] = set()
    lines = []
    for image in images:
        found = _ECR.match(image)
        if found is None or found["host"] in done:
            continue
        done.add(found["host"])
        lines.append(
            f"aws ecr get-login-password --region {shlex.quote(found['region'])} | docker login "
            f"--username AWS --password-stdin {shlex.quote(found['host'])} >/dev/null "
            f'|| fail image_unavailable "couldn\'t sign in to "{shlex.quote(found["host"])}\n'
        )
    return "".join(lines)


def user_data(
    *,
    name: str,
    template: Template,
    environment: str,
    image: str,
    machines: VmMachines,
    once: bool,
    spot: bool,
    token: str,
) -> str:
    config = host_config(
        name=name,
        template=template,
        environment=environment,
        image=image,
        machines=machines,
        once=once,
        spot=spot,
    )
    return _SCRIPT.format(
        name=name,
        environment=environment,
        template=template.name,
        host=HOST,
        scratch=HOST_SCRATCH,
        config=CONFIG,
        config_json=json.dumps(config, sort_keys=True),
        token_file=TOKEN,
        token=token,
        logins=_logins(machines.worker_image, image),
        worker_image=shlex.quote(machines.worker_image),
        uid=UID,
        stop_within=STOP + 30,
    )


def _tags(machines: VmMachines, template: Template, environment: str, **ours: str) -> list[Any]:
    tags = {
        **{str(k): str(v) for k, v in (template.aws.get("tags") or {}).items()},
        OWNER_TAG: machines.owner,
        TEMPLATE_TAG: template.name,
        ENVIRONMENT_TAG: environment,
        **ours,
    }
    return [{"Key": k, "Value": v} for k, v in tags.items()]


def instance_request(
    *,
    launch_id: str,
    name: str,
    template: Template,
    environment: str,
    image: str,
    machines: VmMachines,
    once: bool,
    token: str,
    subnet: str,
    spot: bool,
    attempt: int,
) -> dict[str, Any]:
    """A ``RunInstances`` request for one of the launch's tries: a market (spot or
    on-demand) in a subnet. Each try has a client token of its own, so EC2 starts at most
    one instance for it however often it's sent."""
    aws = template.aws
    disk: dict[str, Any] = {
        "VolumeSize": root_disk(template),
        "VolumeType": "gp3",
        "Encrypted": True,
        "DeleteOnTermination": True,
    }
    if aws.get("kms_key"):
        disk["KmsKeyId"] = aws["kms_key"]
    tags = _tags(machines, template, environment, Name=name, **{LAUNCH_TAG: launch_id})
    request: dict[str, Any] = {
        "ImageId": aws["image"],
        "InstanceType": aws["instance_type"],
        "MinCount": 1,
        "MaxCount": 1,
        "ClientToken": f"{launch_id}-{attempt}",
        "IamInstanceProfile": {"Name": aws["instance_profile"]},
        "NetworkInterfaces": [
            {
                "DeviceIndex": 0,
                "SubnetId": subnet,
                "Groups": list(aws["security_groups"]),
                "AssociatePublicIpAddress": False,
                "DeleteOnTermination": True,
            }
        ],
        "BlockDeviceMappings": [{"DeviceName": aws.get("root_device") or "/dev/xvda", "Ebs": disk}],
        "MetadataOptions": {
            "HttpTokens": "required",
            "HttpPutResponseHopLimit": 1,
            "HttpEndpoint": "enabled",
            "InstanceMetadataTags": "disabled",
        },
        "InstanceInitiatedShutdownBehavior": "terminate",
        "UserData": user_data(
            name=name,
            template=template,
            environment=environment,
            image=image,
            machines=machines,
            once=once,
            spot=spot,
            token=token,
        ),
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": tags},
            {"ResourceType": "volume", "Tags": tags},
            {"ResourceType": "network-interface", "Tags": tags},
        ],
    }
    if spot:
        request["InstanceMarketOptions"] = {
            "MarketType": "spot",
            "SpotOptions": {
                "SpotInstanceType": "one-time",
                "InstanceInterruptionBehavior": "terminate",
            },
        }
    return request


def host_trouble(console: str) -> tuple[str, str] | None:
    """Why a machine's host gave up, from what it wrote to its console: (reason, message)."""
    found = None
    for line in console.splitlines():
        match = _TROUBLE.search(line)
        if match:
            found = (match["reason"], match["message"].strip())
    return found


# --- Talking to EC2 ------------------------------------------------------------------------


class Boto3Ec2:
    """EC2, with the provisioner's AWS credentials, found as boto3 finds them: on EKS, from
    its service account's IAM role."""

    def __init__(self) -> None:
        self._clients: dict[str, Any] = {}

    def _client(self, region: str) -> Any:  # noqa: ANN401 - boto3's client
        if region not in self._clients:
            import boto3
            from botocore.config import Config

            self._clients[region] = boto3.session.Session().client(
                "ec2",
                region_name=region,
                config=Config(
                    retries={"mode": "standard", "max_attempts": 5},
                    connect_timeout=10,
                    read_timeout=30,
                ),
            )
        return self._clients[region]

    def _call(self, region: str, operation: str, **params: Any) -> Any:  # noqa: ANN401
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

        try:
            return getattr(self._client(region), operation)(**params)
        except ClientError as error:
            detail = error.response.get("Error", {})
            raise Ec2Error(
                str(detail.get("Code", "Unknown")), str(detail.get("Message", ""))[:500]
            ) from error
        except NoCredentialsError as error:
            raise Ec2Error("no_credentials", "The provisioner has no AWS credentials.") from error
        except BotoCoreError as error:
            raise Ec2Error("unreachable", str(error)[:500]) from error

    def run_instance(self, region: str, request: dict[str, Any]) -> dict[str, Any]:
        made = self._call(region, "run_instances", **request)
        instance: dict[str, Any] = made["Instances"][0]
        return instance

    def instances(self, region: str, owner: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        token = None
        while True:
            page = self._call(
                region,
                "describe_instances",
                Filters=[{"Name": f"tag:{OWNER_TAG}", "Values": [owner]}],
                MaxResults=1000,
                **({"NextToken": token} if token else {}),
            )
            for reservation in page.get("Reservations") or []:
                found.extend(reservation.get("Instances") or [])
            token = page.get("NextToken")
            if not token:
                return found

    def terminate(self, region: str, instance_ids: list[str]) -> None:
        if not instance_ids:
            return
        try:
            self._call(region, "terminate_instances", InstanceIds=instance_ids)
        except Ec2Error as error:
            if error.code != "InvalidInstanceID.NotFound":
                raise

    def tag(self, region: str, instance_id: str, tags: dict[str, str], remove: list[str]) -> None:
        if tags:
            self._call(
                region,
                "create_tags",
                Resources=[instance_id],
                Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
            )
        if remove:
            self._call(
                region,
                "delete_tags",
                Resources=[instance_id],
                Tags=[{"Key": k} for k in remove],
            )

    def console(self, region: str, instance_id: str) -> str:
        try:
            found = self._call(region, "get_console_output", InstanceId=instance_id, Latest=True)
        except Ec2Error as error:
            if error.code != "UnsupportedOperation":
                raise
            # Only Nitro instances keep the latest output.
            found = self._call(region, "get_console_output", InstanceId=instance_id)
        output = found.get("Output") or ""
        try:
            return base64.b64decode(output).decode("utf-8", "replace")
        except ValueError:
            return str(output)
