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
- A disk for each of the environment's volumes its setup fills (ADR 0076): a new one from
  the version's EBS snapshot, mounted read-only, for a run's machine; an empty one, kept
  when the machine ends so it can be snapshotted, for a setup build's. A volume on a
  shared file system has no disk: the machine mounts the file system, and gives the
  container only the version's directory (ADR 0077).

:func:`cleanup_request` is a small machine's, which removes retired versions from a
shared file system and shuts down, saying so on its console.

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
    "DEVICES",
    "LAUNCH_TAG",
    "OWNER_TAG",
    "REMOVAL_TAG",
    "REMOVED",
    "SETUP_TAG",
    "VOLUME_TAG",
    "Boto3Ec2",
    "Disk",
    "Ec2",
    "Ec2Error",
    "Shared",
    "VmMachines",
    "cleanup_request",
    "disks",
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
#: A setup build's disks and snapshots carry its ID, and the volume's name (ADR 0076).
SETUP_TAG = "iki.ikisci.org/setup"
VOLUME_TAG = "iki.ikisci.org/volume"
#: A machine that removes a setup build's versions from shared file systems carries its
#: ID (ADR 0077), and no launch's.
REMOVAL_TAG = "iki.ikisci.org/removal"
#: What it says on its console once it has.
REMOVED = "IKI-REMOVED"
#: What a setup build's machine says, with a volume's name, once it has copied the disk
#: its script filled into the version's directory in a bucket (ADR 0078).
PUBLISHED = "IKI-PUBLISHED"
#: Its instance type, unless the template says.
CLEANUP_INSTANCE_TYPE = "t3.small"

#: Where things live on a run machine.
HOST = "/opt/iki"
HOST_WORKER = f"{HOST}/bin/iki-worker"
HOST_SCRATCH = "/var/lib/iki/scratch"
#: Where the machine mounts each volume's disk, by the volume's name.
HOST_VOLUMES = "/var/lib/iki/volumes"
#: And each bucket a setup build's disk is copied into.
HOST_BUCKETS = "/var/lib/iki/buckets"
#: The file the worker writes in the scratch directory once the server has taken a setup
#: build as succeeded, so the machine copies its disks into their bucket only then.
SUCCEEDED = ".iki-setup-succeeded"
#: The device names data disks are attached as, in order of their volumes' names. On
#: Nitro instances the kernel names them nvme…; the image's udev rules (Amazon Linux's
#: amazon-ec2-utils) link these names to them.
DEVICES = tuple(f"/dev/sd{letter}" for letter in "fghijklmnopqrstuvwxyz")
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

    # Setup builds' disks and snapshots (ADR 0076).

    def volumes(self, region: str, owner: str) -> list[dict[str, Any]]:
        """The deployment's setup builds' disks in `region`, as EC2 describes them."""

    def delete_volume(self, region: str, volume_id: str) -> None:
        """Deletes the disk; nothing if it's gone already."""

    def create_snapshot(
        self, region: str, volume_id: str, description: str, tags: dict[str, str]
    ) -> dict[str, Any]:
        """Starts a snapshot of the disk; returns it, as EC2 describes it."""

    def snapshots(self, region: str, owner: str) -> list[dict[str, Any]]:
        """The deployment's setup builds' snapshots in `region`, as EC2 describes them."""

    def delete_snapshot(self, region: str, snapshot_id: str) -> None:
        """Deletes the snapshot; nothing if it's gone already."""

    def zones(self, region: str, subnet_ids: list[str]) -> dict[str, str]:
        """Each subnet's availability zone."""

    def fast_restores(self, region: str, snapshot_ids: list[str]) -> list[dict[str, Any]]:
        """Each zone the snapshots are, or are being made, fast to restore in, and its
        state, as EC2 describes them."""

    def enable_fast_restores(self, region: str, snapshot_id: str, zones: list[str]) -> list[str]:
        """Makes the snapshot fast to restore in the zones. Returns why EC2 wouldn't, for
        any it wouldn't."""

    def disable_fast_restores(self, region: str, snapshot_id: str, zones: list[str]) -> None:
        """Stops the snapshot being fast to restore in the zones."""


@dataclass(frozen=True)
class VmMachines:
    """What every run machine gets, from the deployment's settings."""

    #: Where machines reach the server: an address in the VPC, never the cluster's own.
    server_url: str
    worker_image: str
    #: This deployment's name in its instances' tags.
    owner: str
    allow_http: bool = False


@dataclass(frozen=True)
class Disk:
    """A data disk a machine gets for one of its environment's volumes (ADR 0076)."""

    #: The volume's name, where the run's container sees it, and its size in bytes.
    name: str
    mount: str
    size: int
    #: A run's machine gets a new disk from this snapshot, mounted read-only, and deleted
    #: with the machine. A setup build's gets an empty one, writable, kept when the
    #: machine ends so it can be snapshotted.
    snapshot: str | None = None
    #: Or, for a volume in a bucket (ADR 0078), copied into the version's directory there
    #: once the build has succeeded, by the machine before it shuts down.
    publish: Shared | None = None

    @property
    def writable(self) -> bool:
        return self.snapshot is None


@dataclass(frozen=True)
class Shared:
    """A volume's version on a shared file system (ADR 0077): its own directory, under
    `root` in the file system the machine mounts. A setup build's makes it, writable."""

    name: str
    mount: str
    #: Its type (``nfs`` or ``lustre``) and source.
    filesystem: tuple[str, str]
    root: str
    directory: str
    writable: bool


def disks(volumes: dict[str, dict[str, Any]], *, setup: bool = False) -> list[Disk | Shared]:
    """What the machine gets for the environment's volumes, disks in the order they're
    attached: for a run's machine, from :func:`setups.volumes_for`; for a setup build's,
    from its spec."""
    found: list[Disk | Shared] = []
    for name, v in sorted(volumes.items()):
        if "filesystem" in v:
            given = v["filesystem"]
            version = Shared(
                name,
                str(v["mount"]),
                (str(given["type"]), str(given["source"])),
                str(v["root"]),
                str(v["directory"]),
                writable=setup,
            )
            if setup and v.get("strategy") == "object_mount":
                # A bucket can't be written in place: the script fills a disk first.
                found.append(Disk(name, str(v["mount"]), int(v["size"]), publish=version))
            else:
                found.append(version)
        else:
            found.append(Disk(name, str(v["mount"]), int(v["size"]), v.get("snapshot")))
    return found


def _host_volumes(data: list[Disk | Shared]) -> list[dict[str, Any]]:
    """Each volume as ``iki-worker host`` reads it: a disk by its device, or a version's
    directory on a shared file system."""
    found: list[dict[str, Any]] = []
    attached = 0
    for each in data:
        given: dict[str, Any] = {
            "name": each.name,
            "path": f"{HOST_VOLUMES}/{each.name}",
            "mount": each.mount,
            "writable": each.writable,
        }
        if isinstance(each, Disk):
            given["device"] = DEVICES[attached]
            attached += 1
            if each.publish is not None:
                kind, source = each.publish.filesystem
                given["publish"] = {
                    "name": each.name,
                    "filesystem": {"type": kind, "source": source},
                    "root": each.publish.root,
                    "directory": each.publish.directory,
                    "path": f"{HOST_BUCKETS}/{each.name}",
                }
        else:
            kind, source = each.filesystem
            given["filesystem"] = {"type": kind, "source": source}
            given |= {"root": each.root, "directory": each.directory}
        found.append(given)
    return found


def gib(size: int) -> int:
    return max(1, -(-int(size) // 1024**3))


def root_disk(template: Template) -> int:
    """The instance's disk, in GiB: what the template says, or its scratch space and room
    for the system and the run's image."""
    wanted = template.aws.get("root_disk")
    size = (
        parse_quantity(wanted)
        if wanted is not None
        else (template.capacity.scratch_disk or 0) + ROOT_DEFAULT
    )
    return gib(int(size or 0))


def host_config(
    *,
    name: str,
    template: Template,
    environment: str,
    image: str,
    machines: VmMachines,
    once: bool,
    spot: bool,
    data: list[Disk | Shared] | None = None,
) -> dict[str, Any]:
    """What ``iki-worker host`` reads: the run's container, its volumes, and how to stop
    it."""
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
    publishing = any(isinstance(each, Disk) and each.publish for each in data or [])
    if publishing:
        env["IKI_SETUP_SUCCEEDED_FILE"] = f"{SCRATCH}/{SUCCEEDED}"
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
        "volumes": _host_volumes(data or []),
        **({"succeeded_file": f"{HOST_SCRATCH}/{SUCCEEDED}"} if publishing else {}),
    }


_SCRIPT = """\
#!/bin/sh
# An Iki machine (ADR 0074): {name}, for {environment} on {template}.
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
    data: list[Disk | Shared] | None = None,
) -> str:
    config = host_config(
        name=name,
        template=template,
        environment=environment,
        image=image,
        machines=machines,
        once=once,
        spot=spot,
        data=data,
    )
    return _script(name, template, environment, config, token, machines, image)


def _script(
    name: str,
    template: Template,
    environment: str,
    config: dict[str, Any],
    token: str,
    machines: VmMachines,
    *images: str,
) -> str:
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
        logins=_logins(machines.worker_image, *images),
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
    data: list[Disk | Shared] | None = None,
    setup: str | None = None,
) -> dict[str, Any]:
    """A ``RunInstances`` request for one of the launch's tries: a market (spot or
    on-demand) in a subnet. Each try has a client token of its own, so EC2 starts at most
    one instance for it however often it's sent. `data` are the disks for its
    environment's volumes; `setup`, the setup build it's for, whose ID its disks carry."""
    aws = template.aws
    attached = [each for each in data or [] if isinstance(each, Disk)]
    if len(attached) > len(DEVICES):
        raise ValueError(f"A machine can have at most {len(DEVICES)} disks for volumes.")
    ours = {LAUNCH_TAG: launch_id, **({SETUP_TAG: setup} if setup else {})}
    tags = _tags(machines, template, environment, Name=name, **ours)
    request: dict[str, Any] = {
        **_machine(template, subnet, root_disk(template)),
        "InstanceType": aws["instance_type"],
        "ClientToken": f"{launch_id}-{attempt}",
        "BlockDeviceMappings": [
            _root(template, root_disk(template)),
            *(
                {"DeviceName": DEVICES[n], "Ebs": _data_disk(template, each)}
                for n, each in enumerate(attached)
            ),
        ],
        "UserData": user_data(
            name=name,
            template=template,
            environment=environment,
            image=image,
            machines=machines,
            once=once,
            spot=spot,
            token=token,
            data=data,
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


def _root(template: Template, size: int | None) -> dict[str, Any]:
    """The instance's own disk: encrypted, deleted with it, of `size` GiB (its image's
    unless set)."""
    disk: dict[str, Any] = {"VolumeType": "gp3", "Encrypted": True, "DeleteOnTermination": True}
    if size is not None:
        disk["VolumeSize"] = size
    if template.aws.get("kms_key"):
        disk["KmsKeyId"] = template.aws["kms_key"]
    return {"DeviceName": template.aws.get("root_device") or "/dev/xvda", "Ebs": disk}


def _machine(template: Template, subnet: str, size: int | None) -> dict[str, Any]:
    """What every instance the provisioner starts has, whatever it's for."""
    aws = template.aws
    return {
        "ImageId": aws["image"],
        "MinCount": 1,
        "MaxCount": 1,
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
        "BlockDeviceMappings": [_root(template, size)],
        "MetadataOptions": {
            "HttpTokens": "required",
            "HttpPutResponseHopLimit": 1,
            "HttpEndpoint": "enabled",
            "InstanceMetadataTags": "disabled",
        },
        "InstanceInitiatedShutdownBehavior": "terminate",
    }


def cleanup_config(name: str, versions: list[Shared]) -> dict[str, Any]:
    """What ``iki-worker host`` reads on a cleanup machine: the versions to remove, each
    from the file system it mounts."""
    return {
        "name": name,
        "user": f"{UID}:{UID}",
        "worker": HOST_WORKER,
        "remove": [
            {
                "name": each.name,
                "filesystem": {"type": each.filesystem[0], "source": each.filesystem[1]},
                "root": each.root,
                "directory": each.directory,
                "path": f"{HOST_VOLUMES}/{each.name}",
            }
            for each in versions
        ],
    }


def cleanup_request(
    *,
    build_id: str,
    name: str,
    template: Template,
    environment: str,
    machines: VmMachines,
    subnet: str,
    attempt: int,
    versions: list[Shared],
) -> dict[str, Any]:
    """A ``RunInstances`` request for a small on-demand machine that removes a setup
    build's `versions` from their shared file systems and shuts down (ADR 0077). It boots
    the template's image, in its network, with its role, on its cleanup instance type."""
    tags = _tags(
        machines,
        template,
        environment,
        Name=name,
        **{SETUP_TAG: build_id, REMOVAL_TAG: build_id},
    )
    config = cleanup_config(name, versions)
    return {
        **_machine(template, subnet, None),
        "InstanceType": template.aws.get("cleanup_instance_type") or CLEANUP_INSTANCE_TYPE,
        "ClientToken": f"cleanup-{build_id}-{attempt}",
        # No token: it registers with nothing, and only says on its console how it went.
        "UserData": _script(name, template, environment, config, "", machines),
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": tags},
            {"ResourceType": "volume", "Tags": tags},
            {"ResourceType": "network-interface", "Tags": tags},
        ],
    }


def _data_disk(template: Template, disk: Disk) -> dict[str, Any]:
    """A volume's disk: encrypted, of gp3 with the template's IOPS and throughput."""
    given = template.volumes.get(disk.name)
    ebs: dict[str, Any] = {
        "VolumeSize": gib(disk.size),
        "VolumeType": "gp3",
        "Encrypted": True,
        "DeleteOnTermination": not disk.writable,
    }
    if disk.snapshot:
        ebs["SnapshotId"] = disk.snapshot
    if given is not None and given.iops:
        ebs["Iops"] = given.iops
    if given is not None and given.throughput:
        ebs["Throughput"] = given.throughput
    if template.aws.get("kms_key"):
        ebs["KmsKeyId"] = template.aws["kms_key"]
    return ebs


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

    def _pages(
        self,
        region: str,
        operation: str,
        key: str,
        **params: Any,  # noqa: ANN401 - boto3's parameters
    ) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        token = None
        while True:
            page = self._call(
                region, operation, **params, **({"NextToken": token} if token else {})
            )
            found.extend(page.get(key) or [])
            token = page.get("NextToken")
            if not token:
                return found

    def volumes(self, region: str, owner: str) -> list[dict[str, Any]]:
        return self._pages(
            region,
            "describe_volumes",
            "Volumes",
            Filters=[
                {"Name": f"tag:{OWNER_TAG}", "Values": [owner]},
                {"Name": "tag-key", "Values": [SETUP_TAG]},
            ],
            MaxResults=500,
        )

    def delete_volume(self, region: str, volume_id: str) -> None:
        try:
            self._call(region, "delete_volume", VolumeId=volume_id)
        except Ec2Error as error:
            if error.code != "InvalidVolume.NotFound":
                raise

    def create_snapshot(
        self, region: str, volume_id: str, description: str, tags: dict[str, str]
    ) -> dict[str, Any]:
        made: dict[str, Any] = self._call(
            region,
            "create_snapshot",
            VolumeId=volume_id,
            Description=description[:255],
            TagSpecifications=[
                {
                    "ResourceType": "snapshot",
                    "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
                }
            ],
        )
        return made

    def snapshots(self, region: str, owner: str) -> list[dict[str, Any]]:
        return self._pages(
            region,
            "describe_snapshots",
            "Snapshots",
            OwnerIds=["self"],
            Filters=[
                {"Name": f"tag:{OWNER_TAG}", "Values": [owner]},
                {"Name": "tag-key", "Values": [SETUP_TAG]},
            ],
            MaxResults=1000,
        )

    def delete_snapshot(self, region: str, snapshot_id: str) -> None:
        try:
            self._call(region, "delete_snapshot", SnapshotId=snapshot_id)
        except Ec2Error as error:
            if error.code != "InvalidSnapshot.NotFound":
                raise

    def zones(self, region: str, subnet_ids: list[str]) -> dict[str, str]:
        found = self._call(region, "describe_subnets", SubnetIds=subnet_ids)
        return {
            str(subnet["SubnetId"]): str(subnet["AvailabilityZone"])
            for subnet in found.get("Subnets") or []
        }

    def fast_restores(self, region: str, snapshot_ids: list[str]) -> list[dict[str, Any]]:
        if not snapshot_ids:
            return []
        return self._pages(
            region,
            "describe_fast_snapshot_restores",
            "FastSnapshotRestores",
            Filters=[{"Name": "snapshot-id", "Values": snapshot_ids}],
            MaxResults=200,
        )

    def enable_fast_restores(self, region: str, snapshot_id: str, zones: list[str]) -> list[str]:
        made = self._call(
            region,
            "enable_fast_snapshot_restores",
            AvailabilityZones=zones,
            SourceSnapshotIds=[snapshot_id],
        )
        return [
            f"{error.get('Code', 'Unknown')}: {error.get('Message', '')}"
            for item in made.get("Unsuccessful") or []
            for each in item.get("FastSnapshotRestoreStateErrors") or []
            for error in [each.get("Error") or {}]
        ]

    def disable_fast_restores(self, region: str, snapshot_id: str, zones: list[str]) -> None:
        self._call(
            region,
            "disable_fast_snapshot_restores",
            AvailabilityZones=zones,
            SourceSnapshotIds=[snapshot_id],
        )
