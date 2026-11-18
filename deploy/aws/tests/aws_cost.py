"""What a live AWS run cost, or will cost, at list prices.

    make aws-live-cost                  # the last run: build/aws-live/<name>/aws-cost.txt
    PYTHONPATH=deploy/live uv run python deploy/aws/tests/aws_cost.py --estimate

A run's cost is each billable resource's lifetime times its list price. The resources are
the facts the live test records as it starts tearing down (``record_facts`` in
test_aws_live.py: every instance, NAT gateway, address, endpoint, load balancer, database,
key, secret, zone and log group of the deployment's, and its image builds), their lives
are CloudTrail's record of making and deleting each (LookupEvents, in both regions), and
their prices the AWS Price List's (``pricing get-products``). CloudTrail takes up to
15 minutes to record an event: a delete it hasn't yet counts to the end of the window, so
the report right after a run errs high, and ``make aws-live-cost`` later is exact.

``--estimate`` prices the same resources for a run as the live test makes it: the hours
a run usually takes, and the most it can (its cap, plus tearing down).
"""

import argparse
import datetime
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import live_cost
from live_cost import Life, Line

GIB = live_cost.GIB


@dataclass
class Window:
    name: str
    regions: tuple[str, ...]
    start: datetime.datetime
    end: datetime.datetime


@dataclass
class Price:
    usagetype: str
    description: str
    unit: str
    usd: float


#: Each price the report uses: the Price List service, its attribute filters, and the
#: usage type (less its region's prefix) of the one on-demand, first-tier price.
PRICES: dict[str, tuple[str, dict[str, str], str]] = {
    "eks": ("AmazonEKS", {}, r"AmazonEKS-Hours:perCluster"),
    "ec2": (
        "AmazonEC2",
        {
            "operatingSystem": "Linux",
            "tenancy": "Shared",
            "preInstalledSw": "NA",
            "capacitystatus": "Used",
        },
        r"BoxUsage:.*",
    ),
    "ebs-gp3": ("AmazonEC2", {"volumeApiName": "gp3"}, r"EBS:VolumeUsage\.gp3"),
    "nat-hours": ("AmazonEC2", {"productFamily": "NAT Gateway"}, r"NatGateway-Hours"),
    "nat-bytes": ("AmazonEC2", {"productFamily": "NAT Gateway"}, r"NatGateway-Bytes"),
    "ipv4": ("AmazonVPC", {}, r"PublicIPv4:InUseAddress"),
    "endpoint": ("AmazonVPC", {}, r"VpcEndpoint-Hours"),
    "endpoint-bytes": ("AmazonVPC", {}, r"VpcEndpoint-Bytes"),
    "alb": ("AWSELB", {"productFamily": "Load Balancer-Application"}, r"LoadBalancerUsage"),
    "alb-lcu": ("AWSELB", {"productFamily": "Load Balancer-Application"}, r"LCUUsage"),
    "rds": (
        "AmazonRDS",
        {"databaseEngine": "PostgreSQL", "deploymentOption": "Multi-AZ"},
        r"Multi-AZUsage:.*",
    ),
    "rds-gp3": (
        "AmazonRDS",
        {
            "productFamily": "Database Storage",
            "volumeType": "General Purpose-GP3",
            "deploymentOption": "Multi-AZ",
            "databaseEngine": "PostgreSQL",
        },
        r"RDS:Multi-AZ-GP3-Storage",
    ),
    "kms": ("awskms", {}, r"KMS-Keys"),
    "secret": ("AWSSecretsManager", {}, r"AWSSecretsManager-Secrets"),
    "hosted-zone": ("AmazonRoute53", {}, r"HostedZone"),
    "logs": ("AmazonCloudWatch", {"productFamily": "Data Payload"}, r"DataProcessing-Bytes"),
    "codebuild": ("CodeBuild", {}, r"Build-Min:Linux:g1\.large"),
    "ecr": ("AmazonECR", {}, r"TimedStorage-ByteHrs"),
}
#: A usage type's region prefix: USE1-, USW2-, us-east-1-.
_REGION_PREFIX = re.compile(r"^([A-Z]{2,4}[0-9]-|[a-z]{2}-[a-z]+-[0-9]-)")


def pick(products: list[dict[str, Any]], usagetype: str) -> Price | None:
    """The first-tier on-demand price of the product whose usage type is usagetype."""
    pattern = re.compile(usagetype)
    for product in products:
        found = product["product"]["attributes"].get("usagetype", "")
        if not pattern.fullmatch(_REGION_PREFIX.sub("", found)):
            continue
        for term in product.get("terms", {}).get("OnDemand", {}).values():
            for dim in term["priceDimensions"].values():
                if str(dim.get("beginRange", "0")) == "0" and "USD" in dim["pricePerUnit"]:
                    return Price(
                        found, dim["description"], dim["unit"], float(dim["pricePerUnit"]["USD"])
                    )
    return None


class PriceList:
    """The Price List's products, through the AWS CLI (its API is in us-east-1 and two
    other regions only), kept in cache so a report run again asks once."""

    def __init__(self, aws: Callable[..., Any], cache: Path) -> None:
        self.aws = aws
        self.cache = cache
        cache.mkdir(parents=True, exist_ok=True)

    def price(self, key: str, region: str, **attributes: str) -> Price | None:
        service, filters, usagetype = PRICES[key]
        wanted = {**filters, **attributes}
        if service != "AmazonRoute53":
            wanted["regionCode"] = region
        slug = re.sub(r"[^a-z0-9.]+", "-", f"{key}-{region}-{'-'.join(attributes.values())}")
        path = self.cache / f"{slug}.json"
        if path.exists():
            products = json.loads(path.read_text())
        else:
            argv = ["pricing", "get-products", "--service-code", service]
            if wanted:
                argv += [
                    "--filters",
                    *(f"Type=TERM_MATCH,Field={k},Value={v}" for k, v in wanted.items()),
                ]
            found = self.aws(*argv, region="us-east-1")
            products = [json.loads(p) for p in found["PriceList"]]
            path.write_text(json.dumps(products))
        return pick(products, usagetype)


# --- Lifetimes, from CloudTrail -------------------------------------------------------------

#: Each kind of billable resource: the events that make and delete it. An event is the
#: resource's if its ID (the facts') is anywhere in it.
EVENTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "eks cluster": (("CreateCluster",), ("DeleteCluster",)),
    "instance": (("RunInstances",), ("TerminateInstances",)),
    "nat gateway": (("CreateNatGateway",), ("DeleteNatGateway",)),
    "address": (("AllocateAddress",), ("ReleaseAddress",)),
    "vpc endpoint": (("CreateVpcEndpoint",), ("DeleteVpcEndpoints",)),
    "load balancer": (("CreateLoadBalancer",), ("DeleteLoadBalancer",)),
    "db instance": (("CreateDBInstance",), ("DeleteDBInstance",)),
    "kms key": (("CreateKey",), ("ScheduleKeyDeletion",)),
    "secret": (("CreateSecret",), ("DeleteSecret",)),
    "hosted zone": (("CreateHostedZone",), ("DeleteHostedZone",)),
}


def trail(
    aws: Callable[..., Any], window: Window, names: set[str], region: str
) -> list[tuple[str, datetime.datetime, str]]:
    """(event name, time, the event's JSON) for each of names in region, in the window."""
    found = []
    for name in sorted(names):
        listed = aws(
            "cloudtrail",
            "lookup-events",
            "--lookup-attributes",
            f"AttributeKey=EventName,AttributeValue={name}",
            "--start-time",
            window.start.isoformat(),
            "--end-time",
            window.end.isoformat(),
            region=region,
        )
        for event in listed.get("Events", []):
            when = event["EventTime"]
            moment = (
                live_cost.parse_time(when)
                if isinstance(when, str)
                else datetime.datetime.fromtimestamp(when, datetime.UTC)
            )
            found.append((name, moment, event.get("CloudTrailEvent", "")))
    return found


def lives(
    facts: dict[str, Any], events: list[tuple[str, datetime.datetime, str]]
) -> dict[tuple[str, str], Life]:
    """Each resource in the facts, by (kind, ID), with its first making and last deleting."""
    found: dict[tuple[str, str], Life] = {}
    for kind, items in facts.get("resources", {}).items():
        made, deleted = EVENTS[kind]
        for item in items:
            life = found[kind, item["id"]] = Life(kind, item["id"], request=item)
            for event, when, raw in events:
                if (event in made or event in deleted) and item["id"] in raw:
                    # The facts' item stays its request, whichever event is first.
                    life.saw(when, made=event in made, request=item)
            if life.start is None and item.get("launched"):
                life.start = live_cost.parse_time(item["launched"])
    return found


# --- The report -----------------------------------------------------------------------------


@dataclass
class Report(live_cost.Report):
    unpriced: list[str] = field(default_factory=list)

    def footer(self) -> list[str]:
        return [
            "Not priced (cents a run): S3 storage, requests and replication, KMS and "
            "Secrets Manager requests,",
            "CloudWatch metrics, data between zones and regions, RDS backups beyond the "
            "database's size.",
            *self.unpriced,
        ]


def report(
    aws: Callable[..., Any],
    window: Window,
    facts: dict[str, Any],
    cache: Path,
    found: dict[tuple[str, str], Life],
) -> Report:
    """The run's cost: each resource's hours, its quantity, and its price."""
    prices = PriceList(aws, cache)
    result = Report(
        window={
            "name": window.name,
            "start": window.start.isoformat(timespec="seconds"),
            "end": window.end.isoformat(timespec="seconds"),
        }
    )

    def add(
        resource: str,
        key: str,
        region: str,
        hours: float,
        quantity: float,
        note: str = "",
        **attributes: str,
    ) -> None:
        price = prices.price(key, region, **attributes)
        if price is None:
            result.unpriced.append(f"No price for {resource}: {key} {attributes} in {region}.")
            return
        cost = charge(price, quantity, hours)
        result.lines.append(
            Line(
                resource,
                hours,
                quantity,
                price.unit,
                price.usagetype,
                price.description[:60],
                price.usd,
                cost,
                note,
            )
        )

    for (kind, ident), life in sorted(found.items()):
        item = life.request
        region = item.get("region", window.regions[0])
        hours = life.hours(window)
        label = f"{kind} {item.get('label', ident)}"
        if kind == "eks cluster":
            add(label, "eks", region, hours, 1)
        elif kind == "instance":
            add(label, "ec2", region, hours, 1, instanceType=item["type"])
            add(f"{label} disks", "ebs-gp3", region, hours, item.get("disk_gib", 0))
        elif kind == "nat gateway":
            add(label, "nat-hours", region, hours, 1)
            add(f"{label} data", "nat-bytes", region, 1, item.get("bytes", 0) / 1e9)
        elif kind == "address":
            add(label, "ipv4", region, hours, 1)
        elif kind == "vpc endpoint":
            add(label, "endpoint", region, hours, item.get("zones", 1))
        elif kind == "load balancer":
            add(label, "alb", region, hours, 1)
            add(f"{label} LCUs", "alb-lcu", region, hours, 1, note="at least one LCU an hour")
        elif kind == "db instance":
            add(label, "rds", region, hours, 1, instanceType=item["class"])
            add(f"{label} storage", "rds-gp3", region, hours, item.get("storage_gib", 0))
        elif kind == "kms key":
            add(label, "kms", region, hours, 1, note="none charged once pending deletion")
        elif kind == "secret":
            add(label, "secret", region, hours, 1 + item.get("replicas", 0))
        elif kind == "hosted zone":
            free = hours < 12
            add(
                label,
                "hosted-zone",
                region,
                1,
                0 if free else 1,
                note="none charged: deleted within 12 hours" if free else "",
            )
    for build in facts.get("builds", []):
        minutes = -(-build["seconds"] // 60)
        add(f"codebuild {build['id'][-8:]}", "codebuild", window.regions[0], 1, minutes)
    for group, ingested in sorted(facts.get("log_bytes", {}).items()):
        region, _, name = group.partition(" ")
        add(f"logs {name}", "logs", region, 1, ingested / 1e9)
    for region, ingested in sorted(facts.get("endpoint_bytes", {}).items()):
        add("vpc endpoints data", "endpoint-bytes", region, 1, ingested / 1e9)
    image_bytes = facts.get("image_bytes", 0)
    if image_bytes:
        add("ecr images", "ecr", window.regions[0], window_hours(window), image_bytes / 1e9)
    return result


def charge(price: Price, quantity: float, hours: float) -> float:
    """quantity of a price's unit for hours: an hourly price by the hour, a monthly one
    (GB-Mo, and keys and secrets, which are a month each) prorated by the hour, and the
    rest (GB, minutes, a hosted zone) by the unit, whatever the hours."""
    unit = price.unit.lower()
    if unit in ("hrs", "hours", "lcu-hrs"):
        return price.usd * quantity * hours
    if unit in ("keys", "secrets") or unit.endswith(("-mo", "-month")):
        return price.usd * quantity * hours / live_cost.HOURS_PER_MONTH
    return price.usd * quantity


def window_hours(window: Window) -> float:
    return (window.end - window.start).total_seconds() / 3600


# --- An estimate, before a run --------------------------------------------------------------

#: What a run makes (test_aws_live.py), and for how much of it each part lives: the
#: network all of it, the cluster from about 10 minutes in, the database from 20.
INTERFACE_ENDPOINTS = 9  # the network module's default list


def estimate_facts(settings: Any, hours: float) -> dict[str, Any]:
    """The facts a run of hours would record, for --estimate."""
    region, dr = settings.region, settings.dr_region
    zones = len(settings.zones)
    later = max(0.0, hours - 1 / 6)
    resources: dict[str, list[dict[str, Any]]] = {
        "eks cluster": [{"id": settings.name, "region": region, "hours": later}],
        "instance": [
            *(
                {
                    "id": f"system-{i}",
                    "type": settings.system_type,
                    "disk_gib": 52,
                    "region": region,
                    "hours": max(0.0, hours - 0.4),
                }
                for i in range(2)
            ),
            {
                "id": "runs-0",
                "type": settings.runs_type,
                "disk_gib": 52,
                "region": region,
                "hours": 0.5,
            },
        ],
        "nat gateway": [
            {"id": "nat", "region": region, "bytes": 6e9, "hours": hours},
        ],
        "address": [{"id": "nat-ip", "region": region, "hours": hours}],
        "vpc endpoint": [
            {"id": f"endpoint-{i}", "region": region, "zones": zones, "hours": hours}
            for i in range(INTERFACE_ENDPOINTS)
        ],
        "load balancer": [{"id": "alb", "region": region, "hours": max(0.0, hours - 1)}],
        "db instance": [
            {
                "id": settings.name,
                "class": settings.database_class,
                "storage_gib": 20,
                "region": region,
                "hours": max(0.0, hours - 1 / 3),
            }
        ],
        # The bootstrap's, the cluster's two, the network's, the database's three (one in
        # the DR region), the storage's two and the secrets' two.
        "kms key": [
            {"id": f"key-{i}", "region": dr if i in (5, 7, 9) else region, "hours": hours}
            for i in range(11)
        ],
        # Iki's five, the database's three, one optional: each replicated to the DR region.
        "secret": [
            {"id": f"secret-{i}", "region": region, "replicas": 1, "hours": hours} for i in range(9)
        ],
        "hosted zone": [{"id": "zone", "region": region, "hours": hours}],
    }
    return {
        "resources": resources,
        "builds": [{"id": "build-estimate", "seconds": 45 * 60}],
        "log_bytes": {f"{region} all of the run's log groups": 1.5e9},
        "endpoint_bytes": {region: 4e9},
        "image_bytes": 4e9,
    }


def estimate(aws: Callable[..., Any], settings: Any, hours: float, cache: Path) -> Report:
    now = datetime.datetime.now(datetime.UTC)
    window = Window(settings.name, settings.regions, now, now + datetime.timedelta(hours=hours))
    facts = estimate_facts(settings, hours)
    found = {}
    for kind, items in facts["resources"].items():
        for item in items:
            found[kind, item["id"]] = Life(
                kind,
                item["id"],
                start=now,
                end=now + datetime.timedelta(hours=item["hours"]),
                request=item,
            )
    return report(aws, window, facts, cache, found)


def main(argv: list[str] | None = None) -> None:
    """Reports the last run's cost (test_aws_live.Settings), or estimates a run's."""
    import aws_inventory

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--estimate", action="store_true", help="price a run before making it")
    parser.add_argument("--hours", type=float, default=1.75, help="the run's usual hours")
    parser.add_argument("--teardown-hours", type=float, default=0.75)
    args = parser.parse_args(argv)
    settings = aws_inventory.Settings.from_env()
    settings.workdir.mkdir(parents=True, exist_ok=True)
    tools = aws_inventory.Aws(settings)
    tools.token.start()
    try:
        if args.estimate:
            cache = settings.workdir.parent / "prices"
            usual = estimate(tools.aws_json, settings, args.hours, cache)
            capped = estimate(
                tools.aws_json, settings, settings.cap_hours + args.teardown_hours, cache
            )
            sys.stdout.write(usual.text())
            sys.stdout.write(
                f"A usual run, {args.hours:g} hours: {usual.total:.2f} USD. At the cap "
                f"({settings.cap_hours:g} hours, then {args.teardown_hours:g} tearing down): "
                f"{capped.total:.2f} USD.\n"
            )
            return
        saved = json.loads((settings.workdir / "aws-window.json").read_text())
        window = Window(
            settings.name,
            settings.regions,
            live_cost.parse_time(saved["start"]),
            live_cost.parse_time(saved["end"])
            if saved.get("end")
            else datetime.datetime.now(datetime.UTC),
        )
        sys.stdout.write(cost(tools.aws_json, window, settings.workdir))
    finally:
        tools.stop()


def cost(aws: Callable[..., Any], window: Window, workdir: Path) -> str:
    """The run's report from its facts and CloudTrail, written to the work directory."""
    facts_path = workdir / "aws-facts.json"
    facts = json.loads(facts_path.read_text()) if facts_path.exists() else {}
    names = {e for made, deleted in EVENTS.values() for e in (*made, *deleted)}
    events = [e for region in window.regions for e in trail(aws, window, names, region)]
    result = report(aws, window, facts, workdir.parent / "prices", lives(facts, events))
    return live_cost.write(result, workdir, "aws")


if __name__ == "__main__":
    main()
