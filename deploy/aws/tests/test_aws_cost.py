"""aws_cost.py prices a run from its facts and CloudTrail's events: checked offline, with
a few Price List products and events, so it needs no credentials and runs everywhere."""

import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aws_cost
import pytest

START = datetime.datetime(2026, 11, 17, 10, 0, tzinfo=datetime.UTC)
END = START + datetime.timedelta(hours=3)


def product(usagetype: str, unit: str, usd: float, **attributes: str) -> str:
    """A Price List product as get-products prints it: a JSON string."""
    return json.dumps(
        {
            "product": {"attributes": {"usagetype": usagetype, **attributes}},
            "terms": {
                "OnDemand": {
                    "t": {
                        "priceDimensions": {
                            "d": {
                                "unit": unit,
                                "beginRange": "0",
                                "description": f"{usagetype} price",
                                "pricePerUnit": {"USD": str(usd)},
                            }
                        }
                    }
                }
            },
        }
    )


PRODUCTS = {
    "AmazonEKS": [
        product("USE1-AmazonEKS-Hours:extendedSupport", "hours", 0.5),
        product("USE1-AmazonEKS-Hours:perCluster", "Hours", 0.1),
    ],
    "AmazonEC2": [
        product("BoxUsage:m7i-flex.large", "Hrs", 0.09576),
        product("EBS:VolumeUsage.gp3", "GB-Mo", 0.08),
        product("NatGateway-Hours", "Hrs", 0.045),
        product("NatGateway-Bytes", "GB", 0.045),
    ],
    "AmazonRDS": [
        product("Multi-AZUsage:db.t4g.micro", "Hrs", 0.032),
        product("RDS:Multi-AZ-GP3-Storage", "GB-Mo", 0.23),
    ],
    "awskms": [product("us-east-1-KMS-Keys", "Keys", 1.0)],
    "AWSSecretsManager": [product("USE1-AWSSecretsManager-Secrets", "Secrets", 0.4)],
    "AmazonRoute53": [product("HostedZone", "HostedZone", 0.5)],
    "CodeBuild": [
        product("USE1-Build-Min:Linux:g1.small", "minutes", 0.005),
        product("USE1-Build-Min:Linux:g1.large", "minutes", 0.02),
    ],
}


def event(name: str, minutes: float, *ids: str) -> dict[str, Any]:
    when = START + datetime.timedelta(minutes=minutes)
    return {
        "EventName": name,
        "EventTime": when.isoformat(),
        "CloudTrailEvent": json.dumps({"eventName": name, "ids": list(ids)}),
    }


class FakeAws:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str, region: str | None = None) -> Any:
        self.calls.append(args)
        if args[:2] == ("pricing", "get-products"):
            return {"PriceList": PRODUCTS.get(args[args.index("--service-code") + 1], [])}
        if args[:2] == ("cloudtrail", "lookup-events"):
            name = args[args.index("--lookup-attributes") + 1].rsplit("=", 1)[-1]
            return {
                "Events": [e for e in self.events if e["EventName"] == name]
                if region == "us-east-1"
                else []
            }
        raise AssertionError(args)


FACTS = {
    "resources": {
        "eks cluster": [{"id": "iki-test-x", "region": "us-east-1"}],
        "instance": [
            {"id": "i-1", "type": "m7i-flex.large", "disk_gib": 52, "region": "us-east-1"}
        ],
        "nat gateway": [{"id": "nat-1", "region": "us-east-1", "bytes": 2e9}],
        "db instance": [
            {"id": "iki-test-x", "class": "db.t4g.micro", "storage_gib": 20, "region": "us-east-1"}
        ],
        "kms key": [{"id": "k-1", "region": "us-east-1"}],
        "secret": [{"id": "iki-test-x/django", "replicas": 1, "region": "us-east-1"}],
        "hosted zone": [{"id": "Z1", "region": "us-east-1"}],
    },
    "builds": [{"id": "iki-test-x-images:abcdef12", "seconds": 61}],
}

EVENTS = [
    event("CreateCluster", 10, "iki-test-x"),
    event("DeleteCluster", 130, "iki-test-x"),
    event("RunInstances", 25, "i-1"),
    event("TerminateInstances", 115, "i-1"),
    event("CreateNatGateway", 0, "nat-1"),
    event("DeleteNatGateway", 150, "nat-1"),
    event("CreateDBInstance", 20, "iki-test-x"),
    event("DeleteDBInstance", 140, "iki-test-x"),
    event("CreateKey", 0, "k-1"),
    event("ScheduleKeyDeletion", 170, "k-1"),
    event("CreateSecret", 0, "iki-test-x/django"),
    event("DeleteSecret", 160, "iki-test-x/django"),
    event("CreateHostedZone", 1, "/hostedzone/Z1"),
    event("DeleteHostedZone", 150, "Z1"),
]


def window() -> aws_cost.Window:
    return aws_cost.Window("iki-test-x", ("us-east-1", "us-west-2"), START, END)


def test_each_resource_lives_from_its_making_to_its_deleting(tmp_path: Path) -> None:
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "aws-facts.json").write_text(json.dumps(FACTS))
    aws = FakeAws(EVENTS)
    text = aws_cost.cost(aws, window(), tmp_path / "run")
    result = json.loads((tmp_path / "run" / "aws-cost.json").read_text())
    lines = {line["resource"]: line for line in result["lines"]}
    assert lines["eks cluster iki-test-x"]["lifetime_hours"] == 2.0
    assert lines["eks cluster iki-test-x"]["cost"] == pytest.approx(0.2)
    assert lines["instance i-1"]["cost"] == pytest.approx(0.09576 * 1.5)
    assert lines["instance i-1 disks"]["cost"] == pytest.approx(0.08 * 52 * 1.5 / 730)
    assert lines["nat gateway nat-1 data"]["cost"] == pytest.approx(0.045 * 2)
    assert lines["db instance iki-test-x storage"]["cost"] == pytest.approx(0.23 * 20 * 2 / 730)
    assert lines["kms key k-1"]["cost"] == pytest.approx(1.0 * (170 / 60) / 730)
    # The secret and its replica.
    assert lines["secret iki-test-x/django"]["cost"] == pytest.approx(0.4 * 2 * (160 / 60) / 730)
    # A zone deleted within 12 hours isn't charged.
    assert lines["hosted zone Z1"]["cost"] == 0
    # CodeBuild bills by the minute, rounded up.
    assert lines["codebuild abcdef12"]["quantity"] == 2
    assert lines["codebuild abcdef12"]["cost"] == pytest.approx(0.04)
    assert result["total"] == pytest.approx(sum(line["cost"] for line in result["lines"]))
    assert "total" in text and (tmp_path / "run" / "aws-cost.txt").exists()


def test_a_delete_cloudtrail_hasnt_recorded_yet_counts_to_the_windows_end(tmp_path: Path) -> None:
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "aws-facts.json").write_text(json.dumps(FACTS))
    aws_cost.cost(
        FakeAws([e for e in EVENTS if e["EventName"] != "DeleteCluster"]),
        window(),
        tmp_path / "run",
    )
    lines = {
        x["resource"]: x
        for x in json.loads((tmp_path / "run" / "aws-cost.json").read_text())["lines"]
    }
    assert lines["eks cluster iki-test-x"]["lifetime_hours"] == pytest.approx(170 / 60)


def test_prices_are_asked_for_once_and_kept(tmp_path: Path) -> None:
    aws = FakeAws([])
    prices = aws_cost.PriceList(aws, tmp_path)
    first = prices.price("eks", "us-east-1")
    assert first is not None and first.usd == 0.1 and first.usagetype.endswith("perCluster")
    asked = len(aws.calls)
    assert prices.price("eks", "us-east-1") == first and len(aws.calls) == asked


def test_a_usage_type_matches_whole_less_its_region() -> None:
    products = [json.loads(p) for p in PRODUCTS["CodeBuild"]]
    found = aws_cost.pick(products, r"Build-Min:Linux:g1\.large")
    assert found is not None and found.usd == 0.02
    assert aws_cost.pick(products, r"Build-Min:Linux:g1") is None


def test_monthly_prices_are_prorated_by_the_hour() -> None:
    price = aws_cost.Price("x", "x", "GB-Mo", 0.08)
    assert aws_cost.charge(price, 100, 7.3) == pytest.approx(0.08)
    assert aws_cost.charge(aws_cost.Price("x", "x", "Hrs", 0.1), 2, 3) == pytest.approx(0.6)
    assert aws_cost.charge(aws_cost.Price("x", "x", "GB", 0.045), 2, 99) == pytest.approx(0.09)


def test_an_estimate_prices_a_usual_run_and_a_capped_one(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        name="iki-test-x",
        region="us-east-1",
        dr_region="us-west-2",
        regions=("us-east-1", "us-west-2"),
        zones=("us-east-1a", "us-east-1b"),
        system_type="m7i-flex.large",
        runs_type="m7i-flex.large",
        database_class="db.t4g.micro",
    )
    usual = aws_cost.estimate(FakeAws([]), settings, 1.75, tmp_path)
    capped = aws_cost.estimate(FakeAws([]), settings, 2.75, tmp_path)
    assert 0 < usual.total < capped.total
    assert any(line.resource.startswith("eks cluster") for line in usual.lines)
