"""The vm tier's machines on Compute Engine (ADR 0074, 0107): an instance for each launch,
started by the provisioner as :mod:`iki_server.runs.provisioner` decides, and squared with
what Compute Engine says, as :mod:`iki_server.runs.ec2_fleet` does on EC2.

**Starting one.** A launch tries the template's zones in turn, on Spot first if the
template says, then standard if it falls back. A try Compute Engine has no capacity or
quota for moves on to the next; when none has any, the launch ends ``no_capacity``, its
runs say what's missing, and the template backs off before trying again. Anything else
Compute Engine refuses (a role, an image, an instance template, a key) ends it
``refused``, and when that keeps happening, the environment's queued runs fail with what
an administrator can check. An instance template that would give machines a public
address is refused before anything starts.

**Reconciling.** Each round lists the deployment's instances, by their labels:

- One that stopped ends its launch, and is deleted (a stopped instance still costs its
  disk): ``finished`` once its worker had registered and it shut itself down;
  ``reclaimed`` when Compute Engine took it back (a Spot VM preempted, or stopped for host
  maintenance or a host error: its run is retried, and resumes from its checkpoint);
  ``gone`` when someone stopped it; otherwise why, from what its host wrote to its serial
  port when it gave up.
- One that's gone (Compute Engine deletes a preempted Spot VM) ends its launch:
  ``reclaimed`` if its operations say it was preempted, ``gone`` otherwise.
- One still not registered when its token expires is deleted.
- One whose launch ended, or that has none, is deleted.
- One running a run is labelled with the run, its project and team, for billing reports.

Then setup builds' machines, disks and images move on (:mod:`gce_volumes`).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

import structlog
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from iki_server.audit import log as audit
from iki_server.runs import catalog, gce_volumes, launches, setups
from iki_server.runs.ec2 import Disk, Shared, VmMachines, disks, host_trouble
from iki_server.runs.ec2_fleet import vm_machines
from iki_server.runs.gce import (
    LAUNCH_LABEL,
    RUN_LABELS,
    Gce,
    GceError,
    instance_request,
    is_capacity,
    is_refused,
    label,
    machine_name,
    public,
)
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

__all__ = ["PROVISIONERS", "GceFleet"]

logger = structlog.get_logger("iki.provisioner")

PROVISIONERS = ("gcp_gce",)
#: Instance statuses that mean it's stopping or stopped. A stopped instance can't be read
#: from, but still costs its disks.
ENDED = {"STOPPING", "STOPPED", "SUSPENDING", "SUSPENDED", "TERMINATED"}
#: What a host's serial port says it gave up for, as launches end.
HOST_REASONS = {
    "image_unavailable": "image_unavailable",
    "worker_unavailable": "image_unavailable",
    "unsupported": "refused",
}
#: How long an instance template's properties, and an image family's newest image, are
#: taken as they were.
TRUSTED_FOR = timedelta(minutes=10)
#: The operations Compute Engine records when it takes a machine back.
TAKEN_BACK = {
    "compute.instances.preempted": "Compute Engine took its Spot VM back.",
    "compute.instances.terminateOnHostMaintenance": (
        "Compute Engine stopped it for its host's maintenance."
    ),
    "compute.instances.hostError": "Compute Engine stopped it: its host failed.",
}
#: And the ones someone asked for (the API's method names).
STOPPED_BY_SOMEONE = ("stop", "suspend", "delete")


def _market(spot: bool) -> str:
    return "spot" if spot else "on_demand"


def _markets(template: Template) -> list[bool]:
    """Whether each try is Spot, in order."""
    if not template.gcp.get("spot"):
        return [False]
    return [True, False] if template.gcp.get("fallback") == "on_demand" else [True]


def _zones(template: Template) -> list[str]:
    """The template's zones, each launch starting at the next, so machines spread over them."""
    zones = list(template.gcp["zones"])
    live = Launch.objects.filter(template=template.name).exclude(state=LaunchState.ENDED).count()
    start = (live - 1) % len(zones)  # this launch is one of them
    return zones[start:] + zones[:start]


def _what(template: Template, spot: bool) -> str:
    return f"{'a Spot' if spot else 'a standard'} {template.gcp['machine_type']}"


class GceFleet(Fleet):
    """vm-tier machines: Compute Engine instances, with the provisioner's Google credentials."""

    tier = "vm"
    provisioners = PROVISIONERS
    kind = "run machines"

    def __init__(
        self, gce: Gce, machines: VmMachines | None = None, project: str | None = None
    ) -> None:
        self.gce = gce
        self.machines = machines or vm_machines()
        self.project = project or settings.IKI_RUNS_VM_GCP_PROJECT
        #: When each region's setup disks and images were last tidied.
        self.tidied: dict[str, datetime] = {}
        self._known: dict[tuple[str, str], tuple[datetime, Any]] = {}

    def _remembered(self, kind: str, key: str, find: Any) -> Any:  # noqa: ANN401
        now = timezone.now()
        known = self._known.get((kind, key))
        if known is None or now - known[0] > TRUSTED_FOR:
            known = (now, find())
            self._known[(kind, key)] = known
        return known[1]

    def source(self, template: Template) -> str:
        """The instance template its machines start from."""
        return str(
            template.gcp.get("instance_template") or settings.IKI_RUNS_VM_GCP_INSTANCE_TEMPLATE
        ).removeprefix("https://www.googleapis.com/compute/v1/")

    def kms_key(self, template: Template) -> str:
        return str(template.gcp.get("kms_key") or settings.IKI_RUNS_VM_GCP_KMS_KEY)

    def place(self, template: Template) -> str | None:
        return str(template.gcp["region"])

    def spec(self, group: Group, place: str) -> str:
        template = group.template
        request = self._request(
            "-",
            "-",
            group.environment,
            template,
            image=group.image,
            once=group.once,
            token="-",  # noqa: S106 - a placeholder: the digest is of the machine, not its token
            zone=template.gcp["zones"][0],
            spot=bool(template.gcp.get("spot")),
            attempt=0,
            # The image family, not the image it names now: a new one makes a new machine
            # only once the template says so.
            boot_image=str(template.gcp["image"]),
            # A new version of its volumes makes a new machine (ADR 0076).
            data=disks(group.volumes),
        )
        request.pop("requestId")
        request["instance"].pop("name")
        request["instance"]["labels"].pop(LAUNCH_LABEL)
        canonical = json.dumps([place, request], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _request(
        self,
        launch_id: str,
        name: str,
        environment: str,
        template: Template,
        *,
        image: str,
        once: bool,
        token: str,
        zone: str,
        spot: bool,
        attempt: int,
        boot_image: str,
        data: list[Disk | Shared],
        setup: str | None = None,
        source_labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return instance_request(
            launch_id=launch_id,
            name=name,
            machine=machine_name(self.machines.owner, name),
            template=template,
            environment=environment,
            image=image,
            machines=self.machines,
            once=once,
            token=token,
            zone=zone,
            spot=spot,
            attempt=attempt,
            source=self.source(template),
            boot_image=boot_image,
            kms_key=self.kms_key(template),
            data=data,
            setup=setup,
            source_labels=source_labels,
        )

    def launch(self, group: Group, place: str) -> bool:
        template = group.template
        launch, token = launches.create(
            template=template.name,
            provisioner=template.provisioner,
            environment=group.environment,
            plugin_sha=group.plugin_sha,
            image=group.image,
            namespace=place,
            once=group.once,
            spec=self.spec(group, place),
            volumes=setups.provenance(group.volumes),
        )
        return self.start(
            launch,
            template,
            image=group.image,
            once=group.once,
            token=token,
            markets=_markets(template),
            data=disks(group.volumes),
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
        """Starts the launch's instance: each provisioning model in turn, in each zone.
        Returns whether one started; if none did, the launch has ended saying why."""
        source = self.source(template)
        try:
            properties = self._remembered(
                "template", source, lambda: self.gce.template(self.project, source)
            )
            boot = str(template.gcp["image"])
            boot_image = self._remembered("image", boot, lambda: self.gce.image(self.project, boot))
        except GceError as error:
            self._refused(launch, error)
            return False
        if public(properties):
            self._refused(
                launch,
                GceError(
                    "public_address",
                    f"its instance template, {source}, gives machines a public address.",
                ),
            )
            return False
        missing: list[str] = []
        attempt = 0
        for spot in markets:
            for zone in _zones(template):
                request = self._request(
                    str(launch.pk),
                    launch.name,
                    launch.environment,
                    template,
                    image=image,
                    once=once,
                    token=token,
                    zone=zone,
                    spot=spot,
                    attempt=attempt,
                    boot_image=boot_image,
                    data=data,
                    setup=setup,
                    source_labels=properties.get("labels"),
                )
                attempt += 1
                try:
                    self.gce.insert(self.project, zone, request)
                except GceError as error:
                    if is_capacity(error):
                        missing.append(f"{_market(spot)} in {zone}: {error.code}")
                        continue
                    self._refused(launch, error)
                    return False
                self._started(launch, request, zone, spot, template, boot_image)
                return True
        message = _clip(
            f"Waiting for a machine: Compute Engine has no {template.gcp['machine_type']} "
            f"free ({'; '.join(missing)}). Iki tries again shortly."
        )
        launches.end(launch, "no_capacity", message)
        logger.info("no_capacity", launch=str(launch.pk), template=template.name)
        return False

    def _refused(self, launch: Launch, error: GceError) -> None:
        logger.warning("launch_failed", launch=str(launch.pk), error=str(error))
        if is_refused(error):
            message = _clip(f"Compute Engine refused to start it: {error.code}: {error.message}")
            launches.end(launch, "refused", message)
            if launch.setup_id is None:
                give_up_if_hopeless(self, launch, "refused", message, timezone.now())
        else:
            launches.end(launch, "failed", _clip(f"Compute Engine didn't start it: {error}"))

    def _started(
        self,
        launch: Launch,
        request: dict[str, Any],
        zone: str,
        spot: bool,
        template: Template,
        boot_image: str,
    ) -> None:
        name = str(request["instance"]["name"])
        with transaction.atomic():
            Launch.objects.filter(pk=launch.pk).update(
                machine=name,
                market=_market(spot),
                zone=zone,
                message=_clip(f"Starting {_what(template, spot)} in {zone}."),
            )
            # What it runs on, for anyone reviewing how a result was made.
            audit.record(
                "worker.machine_started",
                target=launch,
                data={
                    "instance": name,
                    "machine_type": template.gcp["machine_type"],
                    "image": template.gcp["image"],
                    "resolved_image": boot_image,
                    "instance_template": request["sourceInstanceTemplate"],
                    "market": _market(spot),
                    "zone": zone,
                    "region": launch.namespace,
                    "project": self.project,
                },
            )
        logger.info(
            "launched",
            launch=str(launch.pk),
            template=template.name,
            environment=launch.environment,
            instance=name,
            market=_market(spot),
        )

    # --- Reconciling -------------------------------------------------------------------------

    def listed(self) -> list[dict[str, Any]] | None:
        """The machines the provisioner started: labelled with the deployment, and also with
        a launch, and named for the deployment. The deployment's other instances (its
        cluster's nodes, which deploy/gcp labels with it too) are never Iki's to delete."""
        try:
            found = self.gce.instances(self.project, self.machines.owner)
        except GceError as error:
            logger.warning("reconcile_list_failed", project=self.project, error=str(error))
            return None
        prefix = f"{self.machines.owner}-"
        return [
            each
            for each in found
            if _label(each, LAUNCH_LABEL) and str(each.get("name", "")).startswith(prefix)
        ]

    def reconcile(self, now: datetime) -> int:
        instances = self.listed()
        if instances is None:
            return 0
        live = list(
            Launch.objects.exclude(state=LaunchState.ENDED).filter(provisioner__in=PROVISIONERS)
        )
        ended = 0
        mine = _by_launch(instances)
        for launch in live:
            ended += self._reconcile_one(launch, mine.get(str(launch.pk)), now)
        known = {str(each.pk) for each in live}
        for instance in instances:
            # Its launch ended, or it has none: nothing will ever lease from it. One
            # stopping is going already.
            if _label(instance, LAUNCH_LABEL) not in known and _status(instance) != "STOPPING":
                self._delete(instance["zone"], instance["name"])
        # Environment setup builds, and the images they leave (ADR 0076).
        regions = {each.namespace for each in live} | {
            str(t.gcp["region"])
            for t in catalog.compute().templates.values()
            if t.provisioner in PROVISIONERS
        }
        gce_volumes.tick(self, instances, regions, now)
        return ended

    def _reconcile_one(self, launch: Launch, instance: dict[str, Any] | None, now: datetime) -> int:
        if instance is None:
            # Compute Engine may list a new instance a little after it says it made it.
            if launch.created_at > now - LIST_LAG:
                return 0
            reason, message = self._why_it_went(launch)
            launches.end(launch, reason, message)
            return 1
        status = _status(instance)
        if status in ENDED:
            reason, message = self._why_it_ended(launch, instance)
            self._delete(instance["zone"], instance["name"])
            launches.end(launch, reason, message)
            if reason in ("image_unavailable", "refused"):
                give_up_if_hopeless(self, launch, reason, message, now)
            return 1
        if launch.state == LaunchState.STARTING and launch.expires_at <= now:
            trouble = self._trouble(instance)
            self._delete(instance["zone"], instance["name"])
            launches.end(
                launch,
                "never_started",
                trouble[1]
                if trouble
                else "Its machine started, but its worker didn't register within an hour.",
            )
            return 1
        if not launch.machine:
            Launch.objects.filter(pk=launch.pk).update(
                machine=instance["name"], zone=instance["zone"]
            )
        self._label(launch, instance)
        return 0

    def _taken(self, zone: str, name: str) -> tuple[str, str] | None:
        """Why Compute Engine's operations say the instance stopped, or was deleted, if they
        say."""
        try:
            done = self.gce.history(self.project, zone, name)
        except GceError as error:
            logger.warning("history_failed", instance=name, error=str(error))
            return None
        for kind in done:
            if kind in TAKEN_BACK:
                return "reclaimed", TAKEN_BACK[kind]
        for kind in done:
            if kind.rsplit(".", 1)[-1] in STOPPED_BY_SOMEONE:
                return "gone", "Its Compute Engine instance was stopped or deleted outside Iki."
        return None

    def _why_it_went(self, launch: Launch) -> tuple[str, str]:
        if launch.machine and launch.zone:
            taken = self._taken(launch.zone, launch.machine)
            if taken is not None:
                return taken
        return "gone", "Its Compute Engine instance is gone."

    def _why_it_ended(self, launch: Launch, instance: dict[str, Any]) -> tuple[str, str]:
        taken = self._taken(instance["zone"], instance["name"])
        if taken is not None:
            return taken
        # Nobody else stopped it: it shut itself down.
        if launch.registered_at is not None:
            return "finished", ""
        trouble = self._trouble(instance)
        if trouble is not None:
            return trouble
        return (
            "failed",
            "Its machine shut down before its worker registered. Its serial port says why "
            "(in Cloud Logging, if the project logs serial ports).",
        )

    def _trouble(self, instance: dict[str, Any]) -> tuple[str, str] | None:
        """What the machine's host said it gave up for, from its serial port, if it did."""
        try:
            said = host_trouble(self.gce.console(self.project, instance["zone"], instance["name"]))
        except GceError as error:
            logger.warning("console_failed", instance=instance["name"], error=str(error))
            return None
        if said is None:
            return None
        reason, message = said
        return HOST_REASONS.get(reason, "failed"), _clip(f"Its machine gave up: {message}")

    def _label(self, launch: Launch, instance: dict[str, Any]) -> None:
        have = dict(instance.get("labels") or {})
        wanted = {RUN_LABELS[k]: v for k, v in whose(launch).items()}
        labels = dict(have)
        for key, value in wanted.items():
            if value is None:
                labels.pop(key, None)
            else:
                labels[key] = label(value)
        if labels == have:
            return
        try:
            self.gce.set_labels(
                self.project,
                instance["zone"],
                instance["name"],
                labels,
                str(instance.get("labelFingerprint") or ""),
            )
        except GceError as error:
            logger.warning("label_failed", launch=str(launch.pk), error=str(error))

    def _delete(self, zone: str, name: str) -> None:
        try:
            self.gce.delete(self.project, zone, name)
        except GceError as error:
            logger.warning("delete_failed", instance=name, error=str(error))

    def remove(self, launch: Launch) -> None:
        if launch.machine and launch.zone:
            self._delete(launch.zone, launch.machine)
        # Otherwise the next round finds it by its labels, with its launch ended.

    def check(self, launch: Launch, reason: str) -> str:
        if reason == "refused":
            return (
                "that the provisioner's service account may start the template's instances "
                "from its instance template, with its image, key and machines' service "
                "account, that the instance template gives no public address, and that the "
                "image has Docker"
            )
        return (
            f"that `{launch.image}` and Iki's worker image exist, and that the template's "
            "machines may pull them (from Artifact Registry, their service account needs to "
            "read them)"
        )


def _status(instance: dict[str, Any]) -> str:
    return str(instance.get("status") or "")


def _label(instance: dict[str, Any], key: str) -> str | None:
    found = (instance.get("labels") or {}).get(key)
    return str(found) if found is not None else None


def _by_launch(instances: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each launch's instance: one still going before ones that stopped."""
    found: dict[str, dict[str, Any]] = {}
    for instance in instances:
        launch = _label(instance, LAUNCH_LABEL)
        if not launch:
            continue
        have = found.get(launch)
        if have is None or (_status(have) in ENDED and _status(instance) not in ENDED):
            found[launch] = instance
    return found
