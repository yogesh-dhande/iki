"""What the provisioner asks Compute Engine for (ADR 0074, 0107), and how.

:class:`Gce` is the little of the Compute Engine API the provisioner uses; :class:`GoogleGce`
calls it with the provisioner's own Google credentials (on GKE, its Kubernetes service
account's Google service account, through Workload Identity: Application Default
Credentials, never a key).

:func:`instance_request` is a run machine's ``instances.insert``. Each machine starts from
the deployment's instance template (the ``vm-tier`` module's), which gives it its subnet
with no public address, its service account, and its network tag. The request replaces
what Compute Engine lets a request replace, so a template can't loosen it:

- **Its name** starts with the deployment's (``<owner>-run-<launch>``): the provisioner's
  IAM condition allows only machines, disks and images named so, as Compute Engine's
  labels can't be used in IAM conditions.
- **Labels** naming the deployment, the launch, its template and environment, so the
  provisioner finds its own machines and nobody else's; and, while it runs one, the run,
  its project and team, for billing reports.
- **Shielded VM**: secure boot, a vTPM and integrity monitoring.
- **Its disk**, from the template's image, encrypted with the deployment's key (Cloud KMS),
  deleted with it.
- **Its metadata**: the boot script (``startup-script``), and OS Login with no project-wide
  SSH keys or interactive serial console, as the instance template has them, since
  metadata in a request replaces the template's.
- **Spot** if the template says, deleted when Compute Engine takes it back, with two
  minutes' notice (``preemptionNoticeDuration``) for the run to save its progress.
  Otherwise standard: with GPUs, stopped (never migrated) for host maintenance, as GPUs
  need; without, migrated live, as E2 machines need (they refuse to stop unless Spot).
- No access config, so no public address: :func:`public` refuses an instance template that
  would give one.

Its boot script is EC2's (:func:`iki_server.runs.ec2.script`), with :data:`GCE`'s
differences: ``iki-worker`` is kept under ``/var/lib/iki``, mounted again where programs
may run, as Container-Optimized OS mounts ``/var`` ``noexec`` and its root read-only;
Docker signs in to Artifact Registry with the machine's own token from the metadata server;
and ``iki-worker host`` watches the ``preempted`` metadata value rather than EC2's spot
notice.

Compute Engine answers an insert with an operation, and says only in its result whether the
zone had a machine to give: the client waits for it, briefly.
"""

from __future__ import annotations

import json
import re
import shlex
import uuid
from typing import Any, Protocol

from iki_server.runs.ec2 import (
    Disk,
    Host,
    Shared,
    VmMachines,
    gib,
    host_config,
    script,
)
from iki_spec.compute import ROOT_DEFAULT, Template
from iki_spec.model import parse_quantity

__all__ = [
    "DEVICES",
    "ENVIRONMENT_LABEL",
    "GCE",
    "LAUNCH_LABEL",
    "OWNER_LABEL",
    "RUN_LABELS",
    "SETUP_LABEL",
    "TEMPLATE_LABEL",
    "VOLUME_LABEL",
    "Gce",
    "GceError",
    "GoogleGce",
    "image_request",
    "instance_request",
    "is_capacity",
    "is_refused",
    "label",
    "machine_name",
    "public",
    "region_of",
]

#: Every machine the provisioner starts carries these labels: whose deployment it is (as the
#: vm-tier module labels what it makes), and its launch's ID.
OWNER_LABEL = "iki-deployment"
LAUNCH_LABEL = "iki-launch"
TEMPLATE_LABEL = "iki-template"
ENVIRONMENT_LABEL = "iki-environment"
#: A setup build's disks and images carry its ID, and the volume's name (ADR 0076).
SETUP_LABEL = "iki-setup"
VOLUME_LABEL = "iki-volume"
#: Iki's run labels (:data:`iki_server.runs.machines.RUN_LABELS`) as Compute Engine's: keys
#: of lowercase letters, digits, ``_`` and ``-``.
RUN_LABELS = {
    "iki.ikisci.org/run": "iki-run",
    "iki.ikisci.org/project": "iki-project",
    "iki.ikisci.org/team": "iki-team",
}
#: Data disks are attached under these device names, which the guest environment's udev
#: rules link at ``/dev/disk/by-id/google-<name>``.
DEVICE_NAMES = tuple(f"iki-volume-{n}" for n in range(1, 22))
DEVICES = tuple(f"/dev/disk/by-id/google-{name}" for name in DEVICE_NAMES)
#: Where things live on a machine: all under one directory that's mounted again, over
#: itself, where programs may run.
ROOT = "/var/lib/iki"
DOCKER_CONFIG = f"{ROOT}/docker"
METADATA = "http://metadata.google.internal/computeMetadata/v1"
#: Metadata the instance template sets that a request's metadata replaces.
LOCKED_DOWN = {
    "block-project-ssh-keys": "TRUE",
    "enable-oslogin": "TRUE",
    "serial-port-enable": "FALSE",
}
#: A Spot VM's notice before it's taken back: the two minutes EC2 gives.
PREEMPTION_NOTICE = 120

#: What Compute Engine says when it has no machine to give now: try another zone, the next
#: provisioning model, or later. Quotas and rate limits are here too: they lift as other
#: machines stop, or in a while.
CAPACITY = frozenset(
    {
        "ZONE_RESOURCE_POOL_EXHAUSTED",
        "ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS",
        "REGION_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS",
        "POOL_CAPACITY_INSUFFICIENT",
        "QUOTA_EXCEEDED",
        "quotaExceeded",
        "RESOURCE_OPERATION_RATE_EXCEEDED",
        "rateLimitExceeded",
        "userRateLimitExceeded",
    }
)
#: And what means it will refuse again until someone changes something: a role, an image,
#: an instance template, a key, an organization policy.
REFUSED = frozenset(
    {
        "no_credentials",
        "forbidden",
        "invalid",
        "badRequest",
        "required",
        "notFound",
        "RESOURCE_NOT_FOUND",
        "SERVICE_ACCOUNT_ACCESS_DENIED",
        "conditionNotMet",
        "CONDITION_NOT_MET",
        "UNSUPPORTED_OPERATION",
        "public_address",
    }
)
REFUSED_PREFIXES = ("INVALID", "PERMISSION", "REQUIRED")
#: A zone that has no such machine type, GPU or disk type says it's not found: another
#: zone may have it.
_IN_ZONE = re.compile(r"(machineTypes|acceleratorTypes|diskTypes)/|does not exist in zone")
_REGISTRY = re.compile(r"^(?P<host>[a-z0-9-]+-docker\.pkg\.dev)/")


class GceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"Compute Engine said {code}: {message}")
        self.code = code
        self.message = message


def is_capacity(error: GceError) -> bool:
    if error.code in ("notFound", "RESOURCE_NOT_FOUND") and _IN_ZONE.search(error.message):
        return True
    return error.code in CAPACITY


def is_refused(error: GceError) -> bool:
    if is_capacity(error):
        return False
    return error.code in REFUSED or error.code.startswith(REFUSED_PREFIXES)


class Gce(Protocol):
    def insert(self, project: str, zone: str, request: dict[str, Any]) -> None:
        """Starts the instance `request` asks for, from its instance template, once however
        often it's sent; returns once Compute Engine has made it, or is still making it."""

    def instances(self, project: str, owner: str) -> list[dict[str, Any]]:
        """The deployment's instances in every zone, as Compute Engine describes them (each
        with its ``zone``'s name), stopped ones too."""

    def delete(self, project: str, zone: str, name: str) -> None:
        """Deletes the instance; nothing if it's gone already."""

    def set_labels(
        self, project: str, zone: str, name: str, labels: dict[str, str], fingerprint: str
    ) -> None:
        """Sets the instance's labels to `labels`, if they're still those `fingerprint` says."""

    def console(self, project: str, zone: str, name: str) -> str:
        """What the instance last wrote to its serial port, if Compute Engine has it."""

    def history(self, project: str, zone: str, name: str) -> list[str]:
        """The types of the operations on the instance Compute Engine still keeps, such as
        ``compute.instances.preempted``."""

    def template(self, project: str, source: str) -> dict[str, Any]:
        """The instance template's properties."""

    def image(self, project: str, image: str) -> str:
        """The image `image` names now (a family's newest), by its URL."""

    def disks(self, project: str, owner: str) -> list[dict[str, Any]]:
        """The deployment's disks in every zone, as Compute Engine describes them (each with
        its ``zone``'s name)."""

    def delete_disk(self, project: str, zone: str, name: str) -> None:
        """Deletes the disk; nothing if it's gone already."""

    def create_image(self, project: str, request: dict[str, Any]) -> None:
        """Starts making the image `request` asks for, once however often it's sent."""

    def images(self, project: str, owner: str) -> list[dict[str, Any]]:
        """The deployment's images, as Compute Engine describes them."""

    def delete_image(self, project: str, name: str) -> None:
        """Deletes the image; nothing if it's gone already."""


# --- What a machine boots with ----------------------------------------------------------------


def _logins(*images: str) -> str:
    """Signs Docker in to each Artifact Registry host the images are in, with the machine's
    service account's token from the metadata server (it needs only to read them)."""
    hosts: list[str] = []
    for image in images:
        found = _REGISTRY.match(image)
        if found is not None and found["host"] not in hosts:
            hosts.append(found["host"])
    if not hosts:
        return ""
    lines = [
        f"token=$(curl -sf -H 'Metadata-Flavor: Google' "
        f"{METADATA}/instance/service-accounts/default/token "
        """| sed -n 's/.*"access_token" *: *"\\([^"]*\\)".*/\\1/p') """
        "|| fail image_unavailable \"couldn't get the machine's token\"\n"
    ]
    for host in hosts:
        quoted = shlex.quote(host)
        lines.append(
            "printf '%s' \"$token\" | docker login --username oauth2accesstoken "
            f"--password-stdin https://{quoted} >/dev/null "
            f'|| fail image_unavailable "couldn\'t sign in to "{quoted}\n'
        )
    return "".join(lines)


_PREPARE = f"""\
# Container-Optimized OS mounts /var noexec, and its root read-only: Iki's directory is
# mounted again over itself where programs may run (but not set-user-ID ones or devices).
mkdir -p {ROOT}
mountpoint -q {ROOT} || mount --bind {ROOT} {ROOT} \\
    || fail unsupported "couldn't mount {ROOT}"
mount -o remount,bind,exec,nosuid,nodev {ROOT} \\
    || fail unsupported "couldn't let programs run from {ROOT}"
export DOCKER_CONFIG={DOCKER_CONFIG}
"""

#: Compute Engine's DNS answers at the metadata server's address, as the machine's
#: resolver: a run's container may ask it names (and Cloud DNS's private zones, such as an
#: internal load balancer's), and reach nothing else there. Inserted after the rule that
#: drops the rest, so they come before it.
_DNS = "".join(
    f"iptables -I DOCKER-USER -d 169.254.169.254/32 -p {protocol} --dport 53 -j ACCEPT "
    ">/dev/null 2>&1 || true\n"
    for protocol in ("udp", "tcp")
)

#: Compute Engine's machines.
GCE = Host(
    root=f"{ROOT}/host",
    prepare=_PREPARE,
    logins=_logins,
    devices=DEVICES,
    unit=f"Environment=DOCKER_CONFIG={DOCKER_CONFIG}\n",
    cloud="gcp",
    metadata_allows=_DNS,
)


# --- What it's asked for ------------------------------------------------------------------------


def label(value: str) -> str:
    """`value` as a label's value: lowercase letters, digits, ``_`` and ``-``, at most 63."""
    return re.sub(r"[^a-z0-9_-]", "-", value.lower())[:63]


def machine_name(owner: str, launch_name: str) -> str:
    """A launch's instance's name: the deployment's, then the launch's (``iki-run-<id>``)."""
    return f"{owner}-{launch_name.removeprefix('iki-')}"[:63]


def region_of(zone: str) -> str:
    return zone.rsplit("-", 1)[0]


def _inherited_labels(labels: dict[str, str] | None) -> dict[str, str]:
    """Carry site labels and the deployment's test marker from the source resource.
    Iki assigns ownership and launch IDs to each new resource. Google sets its own labels."""
    return {
        k: v
        for k, v in (labels or {}).items()
        if k == "iki-test" or not k.startswith(("iki-", "goog"))
    }


def _labels(
    machines: VmMachines, template: Template, environment: str, **ours: str
) -> dict[str, str]:
    return {
        **{str(k): str(v) for k, v in (template.gcp.get("labels") or {}).items()},
        OWNER_LABEL: label(machines.owner),
        TEMPLATE_LABEL: label(template.name),
        ENVIRONMENT_LABEL: label(environment),
        **{k: label(v) for k, v in ours.items()},
    }


def _disk_type(template: Template, zone: str) -> str:
    return f"zones/{zone}/diskTypes/{template.gcp.get('disk_type') or 'pd-balanced'}"


def _encrypted(kms_key: str) -> dict[str, Any]:
    return {"diskEncryptionKey": {"kmsKeyName": kms_key}} if kms_key else {}


def instance_request(
    *,
    launch_id: str,
    name: str,
    machine: str,
    template: Template,
    environment: str,
    image: str,
    machines: VmMachines,
    once: bool,
    token: str,
    zone: str,
    spot: bool,
    attempt: int,
    source: str,
    boot_image: str,
    kms_key: str,
    data: list[Disk | Shared] | None = None,
    setup: str | None = None,
    source_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """An ``instances.insert`` for one of the launch's tries: a provisioning model (Spot or
    standard) in a zone. Each try has a request ID of its own, so Compute Engine starts at
    most one instance for it however often it's sent. `source` is the instance template it
    starts from, `boot_image` the image its disk is made from, and `data` the disks for its
    environment's volumes; `setup`, the setup build it's for, whose ID they carry."""
    gcp = template.gcp
    attached = [each for each in data or [] if isinstance(each, Disk)]
    if len(attached) > len(DEVICES):
        raise ValueError(f"A machine can have at most {len(DEVICES)} disks for volumes.")
    if len(attached) != len(data or []):
        raise ValueError("Compute Engine machines get volumes on disks only.")
    ours = {LAUNCH_LABEL: launch_id, **({SETUP_LABEL: setup} if setup else {})}
    labels = {
        **_inherited_labels(source_labels),
        **_labels(machines, template, environment, **ours),
    }
    startup = script(
        name,
        template,
        environment,
        host_config(
            name=name,
            template=template,
            environment=environment,
            image=image,
            machines=machines,
            once=once,
            spot=spot,
            data=data,
            host=GCE,
        ),
        token,
        machines,
        image,
        host=GCE,
    )
    size = root_disk(template)
    instance: dict[str, Any] = {
        "name": machine,
        "description": f"Iki: {name}, for {environment} on {template.name}"[:2048],
        "machineType": f"zones/{zone}/machineTypes/{gcp['machine_type']}",
        "labels": labels,
        "metadata": {
            "items": [
                {"key": "startup-script", "value": startup},
                *({"key": k, "value": v} for k, v in LOCKED_DOWN.items()),
            ]
        },
        "disks": [
            {
                "boot": True,
                "autoDelete": True,
                "initializeParams": {
                    "sourceImage": boot_image,
                    "diskSizeGb": str(max(size, 10)),
                    "diskType": _disk_type(template, zone),
                    "labels": labels,
                },
                **_encrypted(kms_key),
            },
            *(
                _data_disk(template, zone, machine, n, each, labels, kms_key)
                for n, each in enumerate(attached)
            ),
        ],
        "shieldedInstanceConfig": {
            "enableSecureBoot": True,
            "enableVtpm": True,
            "enableIntegrityMonitoring": True,
        },
        "scheduling": _scheduling(spot, gpus=bool(template.capacity.gpu_count)),
    }
    if gcp.get("gpu_type") and template.capacity.gpu_count:
        instance["guestAccelerators"] = [
            {
                "acceleratorType": f"zones/{zone}/acceleratorTypes/{gcp['gpu_type']}",
                "acceleratorCount": template.capacity.gpu_count,
            }
        ]
    return {
        "sourceInstanceTemplate": source,
        "requestId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"iki:{launch_id}:{attempt}")),
        "instance": instance,
    }


def root_disk(template: Template) -> int:
    """The instance's disk, in GiB: what the template says, or its scratch space and room
    for the system and the run's image (as on EC2)."""
    wanted = template.gcp.get("root_disk")
    size = (
        parse_quantity(wanted)
        if wanted is not None
        else (template.capacity.scratch_disk or 0) + ROOT_DEFAULT
    )
    return gib(int(size or 0))


def _scheduling(spot: bool, *, gpus: bool) -> dict[str, Any]:
    """Spot machines are stopped for host maintenance, as Compute Engine requires. Standard
    ones with GPUs too, since GPUs can't be migrated; those without are migrated live, so a
    run goes on through it, and E2 machines refuse to be stopped for it unless Spot."""
    if spot:
        return {
            "provisioningModel": "SPOT",
            "instanceTerminationAction": "DELETE",
            "preemptionNoticeDuration": {"seconds": str(PREEMPTION_NOTICE)},
            "onHostMaintenance": "TERMINATE",
            "automaticRestart": False,
        }
    return {
        "provisioningModel": "STANDARD",
        "onHostMaintenance": "TERMINATE" if gpus else "MIGRATE",
        "automaticRestart": False,
    }


def _data_disk(
    template: Template,
    zone: str,
    machine: str,
    n: int,
    disk: Disk,
    labels: dict[str, str],
    kms_key: str,
) -> dict[str, Any]:
    """A volume's disk (ADR 0076): from the version's image, deleted with the machine, for a
    run's; empty and kept, for a setup build's, so it can be made into an image."""
    params: dict[str, Any] = {
        "diskName": f"{machine}-d{n + 1}"[:63],
        "diskSizeGb": str(max(gib(disk.size), 10)),
        "diskType": _disk_type(template, zone),
        "labels": {**labels, VOLUME_LABEL: label(disk.name)},
    }
    if disk.snapshot:
        params["sourceImage"] = disk.snapshot
    return {
        "deviceName": DEVICE_NAMES[n],
        "autoDelete": not disk.writable,
        "initializeParams": params,
        **_encrypted(kms_key),
    }


def image_request(
    *,
    build_id: str,
    name: str,
    description: str,
    disk: str,
    region: str,
    kms_key: str,
    machines: VmMachines,
    environment: str,
    volume: str,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """An ``images.insert`` for a setup build's filled disk (ADR 0076): kept in the
    template's region, encrypted with the deployment's key, and labelled with its build and
    volume. Its request ID is the build's and volume's, so Compute Engine makes one image
    however often it's sent."""
    return {
        "requestId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"iki:image:{build_id}:{volume}")),
        "image": {
            "name": name[:63].rstrip("-"),
            "description": description[:2048],
            "sourceDisk": disk,
            "storageLocations": [region],
            "labels": {
                **_inherited_labels(labels),
                OWNER_LABEL: label(machines.owner),
                SETUP_LABEL: build_id,
                VOLUME_LABEL: label(volume),
                ENVIRONMENT_LABEL: label(environment),
            },
            **({"imageEncryptionKey": {"kmsKeyName": kms_key}} if kms_key else {}),
        },
    }


def public(properties: dict[str, Any]) -> bool:
    """Whether an instance template would give its machines a public address."""
    return any(
        nic.get("accessConfigs") or nic.get("ipv6AccessConfigs")
        for nic in properties.get("networkInterfaces") or []
    )


def startup_script(instance: dict[str, Any]) -> str:
    """The boot script an instance (or its request) has."""
    for item in (instance.get("metadata") or {}).get("items") or []:
        if item.get("key") == "startup-script":
            return str(item.get("value") or "")
    return ""


# --- Talking to Compute Engine -------------------------------------------------------------------


#: How long to wait for an insert to say whether it got a machine. Most say within seconds;
#: one still going after this is taken as started, and the next rounds see how it went.
INSERT_WAIT = 90.0


class GoogleGce:
    """Compute Engine, with the provisioner's Google credentials, found as Google's clients
    find them (Application Default Credentials): on GKE, its Workload Identity."""

    def __init__(self) -> None:
        self._clients: dict[str, Any] = {}

    def _client(self, kind: str) -> Any:  # noqa: ANN401 - google-cloud-compute's client
        if kind not in self._clients:
            from google.cloud import compute_v1

            self._clients[kind] = getattr(compute_v1, kind)()
        return self._clients[kind]

    def _call(self, kind: str, method: str, *args: Any, **params: Any) -> Any:  # noqa: ANN401
        from google.api_core import exceptions
        from google.auth import exceptions as auth

        try:
            return getattr(self._client(kind), method)(*args, **params)
        except exceptions.GoogleAPICallError as error:
            raise _error(error) from error
        except auth.DefaultCredentialsError as error:
            raise GceError(
                "no_credentials", "The provisioner has no Google credentials."
            ) from error
        except auth.GoogleAuthError as error:
            raise GceError("no_credentials", str(error)[:500]) from error
        except exceptions.RetryError as error:
            raise GceError("unreachable", str(error)[:500]) from error
        except OSError as error:
            raise GceError("unreachable", str(error)[:500]) from error

    def _wait(self, operation: Any, timeout: float) -> None:  # noqa: ANN401 - an operation
        import concurrent.futures

        from google.api_core import exceptions

        try:
            operation.result(timeout=timeout)
        except exceptions.GoogleAPICallError as error:
            raise _error(error) from error
        except concurrent.futures.TimeoutError:
            pass  # still going: the next rounds see

    def insert(self, project: str, zone: str, request: dict[str, Any]) -> None:
        from google.cloud import compute_v1

        made = compute_v1.InsertInstanceRequest(
            project=project,
            zone=zone,
            source_instance_template=request["sourceInstanceTemplate"],
            request_id=request["requestId"],
            instance_resource=compute_v1.Instance.from_json(json.dumps(request["instance"])),
        )
        try:
            operation = self._call("InstancesClient", "insert", request=made)
            self._wait(operation, INSERT_WAIT)
        except GceError as error:
            if error.code != "alreadyExists":
                raise

    def instances(self, project: str, owner: str) -> list[dict[str, Any]]:
        from google.cloud import compute_v1

        pages = self._call(
            "InstancesClient",
            "aggregated_list",
            request=compute_v1.AggregatedListInstancesRequest(
                project=project,
                filter=f'labels.{OWNER_LABEL} = "{label(owner)}"',
            ),
        )
        found: list[dict[str, Any]] = []
        try:
            for _, scoped in pages:
                for instance in scoped.instances:
                    described = json.loads(compute_v1.Instance.to_json(instance))
                    described["zone"] = str(described.get("zone", "")).rsplit("/", 1)[-1]
                    found.append(described)
        except Exception as error:
            from google.api_core import exceptions

            if isinstance(error, exceptions.GoogleAPICallError):
                raise _error(error) from error
            raise
        return found

    def delete(self, project: str, zone: str, name: str) -> None:
        try:
            self._call("InstancesClient", "delete", project=project, zone=zone, instance=name)
        except GceError as error:
            if error.code not in ("notFound", "RESOURCE_NOT_FOUND"):
                raise

    def set_labels(
        self, project: str, zone: str, name: str, labels: dict[str, str], fingerprint: str
    ) -> None:
        from google.cloud import compute_v1

        self._call(
            "InstancesClient",
            "set_labels",
            project=project,
            zone=zone,
            instance=name,
            instances_set_labels_request_resource=compute_v1.InstancesSetLabelsRequest(
                labels=labels, label_fingerprint=fingerprint
            ),
        )

    def console(self, project: str, zone: str, name: str) -> str:
        found = self._call(
            "InstancesClient",
            "get_serial_port_output",
            project=project,
            zone=zone,
            instance=name,
            port=1,
        )
        return str(found.contents or "")

    def history(self, project: str, zone: str, name: str) -> list[str]:
        from google.cloud import compute_v1

        link = f"https://www.googleapis.com/compute/v1/projects/{project}/zones/{zone}/instances/{name}"
        pages = self._call(
            "ZoneOperationsClient",
            "list",
            request=compute_v1.ListZoneOperationsRequest(
                project=project, zone=zone, filter=f'targetLink = "{link}"'
            ),
        )
        return [str(each.operation_type) for each in pages]

    def template(self, project: str, source: str) -> dict[str, Any]:
        from google.cloud import compute_v1

        parts = source.removeprefix("https://www.googleapis.com/compute/v1/").split("/")
        # projects/<p>/regions/<r>/instanceTemplates/<n>, or projects/<p>/global/...
        if len(parts) == 6 and parts[2] == "regions":
            found = self._call(
                "RegionInstanceTemplatesClient",
                "get",
                project=parts[1],
                region=parts[3],
                instance_template=parts[5],
            )
        elif len(parts) == 5 and parts[2] == "global":
            found = self._call(
                "InstanceTemplatesClient", "get", project=parts[1], instance_template=parts[4]
            )
        else:
            raise GceError("invalid", f"{source} isn't an instance template.")
        properties: dict[str, Any] = (
            json.loads(compute_v1.InstanceTemplate.to_json(found)).get("properties") or {}
        )
        return properties

    def image(self, project: str, image: str) -> str:
        parts = image.split("/")
        # projects/<p>/global/images/family/<f>, or projects/<p>/global/images/<i>
        if len(parts) == 6 and parts[4] == "family":
            found = self._call("ImagesClient", "get_from_family", project=parts[1], family=parts[5])
            return str(found.self_link)
        return image

    def disks(self, project: str, owner: str) -> list[dict[str, Any]]:
        from google.cloud import compute_v1

        pages = self._call(
            "DisksClient",
            "aggregated_list",
            request=compute_v1.AggregatedListDisksRequest(
                project=project, filter=f'labels.{OWNER_LABEL} = "{label(owner)}"'
            ),
        )
        found: list[dict[str, Any]] = []
        for _, scoped in self._pages(pages):
            for disk in scoped.disks:
                described = json.loads(compute_v1.Disk.to_json(disk))
                described["zone"] = str(described.get("zone", "")).rsplit("/", 1)[-1]
                found.append(described)
        return found

    def delete_disk(self, project: str, zone: str, name: str) -> None:
        try:
            self._call("DisksClient", "delete", project=project, zone=zone, disk=name)
        except GceError as error:
            if error.code not in ("notFound", "RESOURCE_NOT_FOUND"):
                raise

    def create_image(self, project: str, request: dict[str, Any]) -> None:
        from google.cloud import compute_v1

        made = compute_v1.InsertImageRequest(
            project=project,
            request_id=request["requestId"],
            image_resource=compute_v1.Image.from_json(json.dumps(request["image"])),
        )
        try:
            # Not waited for: it takes as long as the disk takes to copy.
            self._call("ImagesClient", "insert", request=made)
        except GceError as error:
            if error.code != "alreadyExists":
                raise

    def images(self, project: str, owner: str) -> list[dict[str, Any]]:
        from google.cloud import compute_v1

        pages = self._call(
            "ImagesClient",
            "list",
            request=compute_v1.ListImagesRequest(
                project=project, filter=f'labels.{OWNER_LABEL} = "{label(owner)}"'
            ),
        )
        return [json.loads(compute_v1.Image.to_json(each)) for each in self._pages(pages)]

    def delete_image(self, project: str, name: str) -> None:
        try:
            self._call("ImagesClient", "delete", project=project, image=name)
        except GceError as error:
            if error.code not in ("notFound", "RESOURCE_NOT_FOUND"):
                raise

    def _pages(self, pages: Any) -> list[Any]:  # noqa: ANN401 - a pager
        """Every page's items: a pager asks for the next page as it's read."""
        from google.api_core import exceptions

        try:
            return list(pages)
        except exceptions.GoogleAPICallError as error:
            raise _error(error) from error


def _error(error: Any) -> GceError:  # noqa: ANN401 - a GoogleAPICallError
    """Compute Engine's error, by its code: an operation's (``ZONE_RESOURCE_POOL_EXHAUSTED``),
    or the HTTP error's reason (``forbidden``)."""
    code = ""
    for each in getattr(error, "errors", None) or []:
        found = each.get("reason") if isinstance(each, dict) else getattr(each, "code", "")
        if found:
            code = str(found)
            break
    if not code:
        code = str(getattr(error, "reason", "") or "") or f"http_{getattr(error, 'code', '')}"
    return GceError(code, str(getattr(error, "message", "") or error)[:500])
