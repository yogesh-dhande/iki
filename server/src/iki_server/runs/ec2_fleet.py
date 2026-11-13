"""The vm tier's machines (ADR 0074): an EC2 instance for each launch, started by the
provisioner as :mod:`iki_server.runs.provisioner` decides, and squared with what EC2 says.

**Starting one.** A launch tries the template's subnets in turn (each a zone), on spot
first if the template says, then on-demand if it falls back. A try EC2 has no capacity
for moves on to the next; when none has any, the launch ends ``no_capacity``, its runs
say what's missing, and the template backs off before trying again. Anything else EC2
refuses (a policy, an image, a subnet) ends it ``refused``, and when that keeps happening,
the environment's queued runs fail with what an administrator can check.

**Reconciling.** Each round lists the deployment's instances in each region its templates
use, by their tags:

- An instance that ended ends its launch: ``finished`` once its worker had registered and
  it shut itself down; ``reclaimed`` when AWS took back its spot instance (its run is
  retried, and resumes from its checkpoint); otherwise why, from what its host wrote to
  its console when it gave up (an image it couldn't pull, say).
- One still not registered when its token expires is terminated.
- One whose launch ended, or that has none, is terminated.
- One running a run is tagged with the run, its project and team, for AWS's cost reports.

Then setup builds' machines, disks and snapshots move on (:mod:`ec2_volumes`).

**Volumes.** A machine for an environment whose setup fills volumes gets a new disk for
each, from the newest version's EBS snapshot, mounted read-only (ADR 0076). Until a
version is ready, no machine starts, and its runs say why.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

import structlog
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from iki_server.audit import log as audit
from iki_server.runs import catalog, ec2_volumes, launches, setups
from iki_server.runs.ec2 import (
    LAUNCH_TAG,
    REMOVAL_TAG,
    SETUP_TAG,
    Disk,
    Ec2,
    Ec2Error,
    Shared,
    VmMachines,
    disks,
    host_trouble,
    instance_request,
    is_capacity,
    is_refused,
)
from iki_server.runs.machines import RUN_LABELS
from iki_server.runs.models import Launch, LaunchState
from iki_server.runs.provisioner import (
    LIST_LAG,
    Fleet,
    Group,
    _clip,
    give_up_if_hopeless,
    whose,
)
from iki_spec.compute import Template

__all__ = ["PROVISIONERS", "Ec2Fleet", "vm_machines"]

logger = structlog.get_logger("iki.provisioner")

PROVISIONERS = ("aws_ec2",)
#: Instance states that mean it's going or gone.
ENDED = {"shutting-down", "terminated", "stopping", "stopped"}
#: What a host's console says it gave up for, as launches end.
HOST_REASONS = {
    "image_unavailable": "image_unavailable",
    "worker_unavailable": "image_unavailable",
    "unsupported": "refused",
}


def vm_machines() -> VmMachines:
    return VmMachines(
        server_url=settings.IKI_RUNS_VM_SERVER_URL,
        worker_image=settings.IKI_RUNS_WORKER_IMAGE,
        owner=settings.IKI_RUNS_VM_OWNER,
        allow_http=settings.IKI_RUNS_ALLOW_HTTP,
    )


def _market(spot: bool) -> str:
    return "spot" if spot else "on_demand"


def _markets(template: Template) -> list[bool]:
    """Whether each try is spot, in order."""
    if not template.aws.get("spot"):
        return [False]
    return [True, False] if template.aws.get("fallback") == "on_demand" else [True]


def _subnets(template: Template) -> list[str]:
    """The template's subnets, each launch starting at the next, so machines spread over its
    zones."""
    subnets = list(template.aws["subnets"])
    live = Launch.objects.filter(template=template.name).exclude(state=LaunchState.ENDED).count()
    start = (live - 1) % len(subnets)  # this launch is one of them
    return subnets[start:] + subnets[:start]


def _what(template: Template, spot: bool) -> str:
    return f"{'a spot' if spot else 'an on-demand'} {template.aws['instance_type']}"


class Ec2Fleet(Fleet):
    """vm-tier machines: EC2 instances, with the provisioner's AWS credentials."""

    tier = "vm"
    provisioners = PROVISIONERS
    kind = "run machines"

    def __init__(self, ec2: Ec2, machines: VmMachines | None = None) -> None:
        self.ec2 = ec2
        self.machines = machines or vm_machines()
        #: When each region's setup disks and snapshots were last tidied.
        self.tidied: dict[str, datetime] = {}
        self._zones: dict[tuple[str, ...], dict[str, str]] = {}

    def zones(self, template: Template) -> dict[str, str]:
        """Each of the template's subnets' zones."""
        key = (str(template.aws["region"]), *template.aws["subnets"])
        if key not in self._zones:
            self._zones[key] = self.ec2.zones(key[0], list(key[1:]))
        return self._zones[key]

    def place(self, template: Template) -> str | None:
        return str(template.aws["region"])

    def spec(self, group: Group, place: str) -> str:
        template = group.template
        request = instance_request(
            launch_id="-",
            name="-",
            template=template,
            environment=group.environment,
            image=group.image,
            machines=self.machines,
            once=group.once,
            token="-",  # noqa: S106 - a placeholder: the digest is of the machine, not its token
            subnet=template.aws["subnets"][0],
            spot=bool(template.aws.get("spot")),
            attempt=0,
            # A new version of its volumes makes a new machine (ADR 0076).
            data=disks(setups.volumes_for(group.environment, place) or {}),
        )
        canonical = json.dumps([place, request], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def launch(self, group: Group, place: str) -> bool:
        template = group.template
        volumes = setups.volumes_for(group.environment, place)
        if volumes is None:
            # Its setup fills volumes, and no version is ready here: its runs say so.
            return False
        launch, token = launches.create(
            template=template.name,
            provisioner=template.provisioner,
            environment=group.environment,
            plugin_sha=group.plugin_sha,
            image=group.image,
            namespace=place,
            once=group.once,
            spec=self.spec(group, place),
            volumes=setups.provenance(volumes),
        )
        return self.start(
            launch,
            template,
            image=group.image,
            once=group.once,
            token=token,
            markets=_markets(template),
            data=disks(volumes),
        )

    def start(
        self,
        launch: Launch,
        template: Template,
        *,
        image: str,
        once: bool,
        token: str,
        markets: list[bool],
        data: list[Disk | Shared],
        setup: str | None = None,
    ) -> bool:
        """Starts the launch's instance: each market in turn, in each subnet. Returns whether
        one started; if none did, the launch has ended saying why."""
        missing: list[str] = []
        attempt = 0
        for spot in markets:
            for subnet in _subnets(template):
                request = instance_request(
                    launch_id=str(launch.pk),
                    name=launch.name,
                    template=template,
                    environment=launch.environment,
                    image=image,
                    machines=self.machines,
                    once=once,
                    token=token,
                    subnet=subnet,
                    spot=spot,
                    attempt=attempt,
                    data=data,
                    setup=setup,
                )
                attempt += 1
                try:
                    instance = self.ec2.run_instance(launch.namespace, request)
                except Ec2Error as error:
                    if is_capacity(error):
                        missing.append(f"{_market(spot)} in {subnet}: {error.code}")
                        continue
                    self._refused(launch, error)
                    return False
                self._started(launch, instance, spot, template)
                return True
        message = _clip(
            f"Waiting for a machine: EC2 has no {template.aws['instance_type']} free "
            f"({'; '.join(missing)}). Iki tries again shortly."
        )
        launches.end(launch, "no_capacity", message)
        logger.info("no_capacity", launch=str(launch.pk), template=template.name)
        return False

    def _refused(self, launch: Launch, error: Ec2Error) -> None:
        logger.warning("launch_failed", launch=str(launch.pk), error=str(error))
        if is_refused(error):
            message = _clip(f"EC2 refused to start it: {error.code}: {error.message}")
            launches.end(launch, "refused", message)
            if launch.setup_id is None:
                give_up_if_hopeless(self, launch, "refused", message, timezone.now())
        else:
            launches.end(launch, "failed", _clip(f"EC2 didn't start it: {error}"))

    def _started(
        self, launch: Launch, instance: dict[str, Any], spot: bool, template: Template
    ) -> None:
        zone = str((instance.get("Placement") or {}).get("AvailabilityZone", ""))
        with transaction.atomic():
            Launch.objects.filter(pk=launch.pk).update(
                machine=str(instance["InstanceId"]),
                market=_market(spot),
                zone=zone,
                message=_clip(
                    f"Starting {_what(template, spot)}" + (f" in {zone}." if zone else ".")
                ),
            )
            # What it runs on, for anyone reviewing how a result was made.
            audit.record(
                "worker.machine_started",
                target=launch,
                data={
                    "instance": str(instance["InstanceId"]),
                    "instance_type": template.aws["instance_type"],
                    "image": template.aws["image"],
                    "resolved_image": str(instance.get("ImageId", "")),
                    "market": _market(spot),
                    "zone": zone,
                    "subnet": str(instance.get("SubnetId", "")),
                    "region": launch.namespace,
                },
            )
        logger.info(
            "launched",
            launch=str(launch.pk),
            template=template.name,
            environment=launch.environment,
            instance=instance["InstanceId"],
            market=_market(spot),
        )

    # --- Reconciling -------------------------------------------------------------------------

    def reconcile(self, now: datetime) -> int:
        live = list(
            Launch.objects.exclude(state=LaunchState.ENDED).filter(provisioner__in=PROVISIONERS)
        )
        regions = {each.namespace for each in live} | {
            str(t.aws["region"])
            for t in catalog.compute().templates.values()
            if t.provisioner in PROVISIONERS
        }
        ended = 0
        listed: dict[str, list[dict[str, Any]]] = {}
        for region in sorted(regions):
            try:
                instances = self.ec2.instances(region, self.machines.owner)
            except Ec2Error as error:
                logger.warning("reconcile_list_failed", region=region, error=str(error))
                continue
            listed[region] = instances
            mine = _by_launch(instances)
            here = [each for each in live if each.namespace == region]
            for launch in here:
                ended += self._reconcile_one(launch, mine.get(str(launch.pk)), now)
            known = {str(each.pk) for each in here}
            stray = [
                i["InstanceId"]
                for i in instances
                if _state(i) not in ENDED
                and _tag(i, LAUNCH_TAG) not in known
                # A machine removing versions from a shared file system has no launch: its
                # own round ends it (ADR 0077).
                and _tag(i, REMOVAL_TAG) is None
                # Nor one copying its setup build's disks into a bucket once its worker
                # has gone (ADR 0078): it shuts down when it's done.
                and not ec2_volumes.copying(_tag(i, SETUP_TAG))
            ]
            if stray:
                # Its launch ended, or it has none: nothing will ever lease from it.
                self._terminate(region, stray)
        # Environment setup builds, and the snapshots they leave (ADR 0076).
        ec2_volumes.tick(self, listed, now)
        return ended

    def _reconcile_one(self, launch: Launch, instance: dict[str, Any] | None, now: datetime) -> int:
        if instance is None:
            # EC2 lists a new instance a little after it says it started it.
            if launch.created_at > now - LIST_LAG:
                return 0
            launches.end(launch, "gone", "Its EC2 instance is gone.")
            return 1
        state = _state(instance)
        if state in ENDED:
            reason, message = self._why_it_ended(launch, instance)
            if state in ("stopping", "stopped"):
                self._terminate(launch.namespace, [instance["InstanceId"]])
            launches.end(launch, reason, message)
            if reason in ("image_unavailable", "refused"):
                give_up_if_hopeless(self, launch, reason, message, now)
            return 1
        if launch.state == LaunchState.STARTING and launch.expires_at <= now:
            self._terminate(launch.namespace, [instance["InstanceId"]])
            trouble = self._trouble(launch.namespace, instance)
            launches.end(
                launch,
                "never_started",
                trouble[1]
                if trouble
                else "Its machine started, but its worker didn't register within an hour.",
            )
            return 1
        if not launch.machine:
            Launch.objects.filter(pk=launch.pk).update(machine=instance["InstanceId"])
        self._tag(launch, instance)
        return 0

    def _why_it_ended(self, launch: Launch, instance: dict[str, Any]) -> tuple[str, str]:
        reason = instance.get("StateReason") or {}
        code = str(reason.get("Code", ""))
        if code.startswith("Server.SpotInstance"):
            return "reclaimed", "AWS took its spot instance back."
        if code == "Server.InsufficientInstanceCapacity":
            return "no_capacity", "Waiting for a machine: EC2 had no capacity to start it."
        if code == "Client.InstanceInitiatedShutdown":
            if launch.registered_at is not None:
                return "finished", ""
            trouble = self._trouble(launch.namespace, instance)
            if trouble is not None:
                return trouble
            return "failed", "Its machine shut down before its worker registered."
        if code == "Client.UserInitiatedShutdown":
            return "gone", "Its EC2 instance was stopped or terminated outside Iki."
        detail = str(reason.get("Message") or code or _state(instance))
        return "failed", _clip(f"Its EC2 instance ended: {detail}")

    def _trouble(self, region: str, instance: dict[str, Any]) -> tuple[str, str] | None:
        """What the machine's host said it gave up for, from its console, if it did."""
        try:
            said = host_trouble(self.ec2.console(region, instance["InstanceId"]))
        except Ec2Error as error:
            logger.warning("console_failed", instance=instance["InstanceId"], error=str(error))
            return None
        if said is None:
            return None
        reason, message = said
        return HOST_REASONS.get(reason, "failed"), _clip(f"Its machine gave up: {message}")

    def _tag(self, launch: Launch, instance: dict[str, Any]) -> None:
        have = {t["Key"]: t["Value"] for t in instance.get("Tags") or []}
        wanted = whose(launch)
        tags = {k: v for k, v in wanted.items() if v is not None and have.get(k) != v}
        remove = [k for k in RUN_LABELS if wanted.get(k) is None and k in have]
        if not tags and not remove:
            return
        try:
            self.ec2.tag(launch.namespace, instance["InstanceId"], tags, remove)
        except Ec2Error as error:
            logger.warning("tag_failed", launch=str(launch.pk), error=str(error))

    def _terminate(self, region: str, instance_ids: list[str]) -> None:
        try:
            self.ec2.terminate(region, instance_ids)
        except Ec2Error as error:
            logger.warning("terminate_failed", instances=instance_ids, error=str(error))

    def remove(self, launch: Launch) -> None:
        if launch.machine:
            self._terminate(launch.namespace, [launch.machine])
        # Otherwise the next round finds it by its tags, with its launch ended.

    def check(self, launch: Launch, reason: str) -> str:
        if reason == "refused":
            return (
                "that the provisioner's IAM role may start the template's instances, with its "
                "image, subnets, security groups and instance profile, and that the image has "
                "Docker"
            )
        return (
            f"that `{launch.image}` and Iki's worker image exist, and that the template's "
            "instances may pull them (from ECR, their instance profile needs to read them)"
        )


def _state(instance: dict[str, Any]) -> str:
    return str((instance.get("State") or {}).get("Name", ""))


def _tag(instance: dict[str, Any], key: str) -> str | None:
    for tag in instance.get("Tags") or []:
        if tag.get("Key") == key:
            return str(tag.get("Value"))
    return None


def _by_launch(instances: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each launch's instance: one still going before ones that ended."""
    found: dict[str, dict[str, Any]] = {}
    for instance in instances:
        launch = _tag(instance, LAUNCH_TAG)
        if not launch:
            continue
        have = found.get(launch)
        if have is None or (_state(have) in ENDED and _state(instance) not in ENDED):
            found[launch] = instance
    return found
