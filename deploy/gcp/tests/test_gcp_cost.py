"""gcp_cost.py prices a run from its audit log entries: checked offline, with a fake
project and a catalog of a few SKUs, so it needs no credentials and runs everywhere."""

import datetime
import json
from pathlib import Path
from typing import Any

import gcp_cost
import pytest

START = datetime.datetime(2026, 11, 17, 10, 0, tzinfo=datetime.UTC)
END = START + datetime.timedelta(hours=3)


def sku(sku_id: str, description: str, unit: str, usd: float, *, free: bool = False) -> Any:
    units, nanos = int(usd), round((usd - int(usd)) * 1e9)
    tiers = [{"startUsageAmount": 0, "unitPrice": {"units": str(units), "nanos": nanos}}]
    if free:
        tiers.insert(0, {"startUsageAmount": 0, "unitPrice": {"units": "0", "nanos": 0}})
        tiers[1]["startUsageAmount"] = 50
    return {
        "skuId": sku_id,
        "description": description,
        "category": {"usageType": "OnDemand"},
        "serviceRegions": ["us-central1"],
        "pricingInfo": [{"pricingExpression": {"usageUnit": unit, "tieredRates": tiers}}],
    }


def entry(minutes: float, method: str, resource: str, request: Any = None) -> Any:
    when = START + datetime.timedelta(minutes=minutes)
    return {
        "timestamp": when.isoformat().replace("+00:00", "Z"),
        "protoPayload": {"methodName": method, "resourceName": resource, "request": request},
    }


class FakeProject:
    def __init__(self, entries: list[Any]) -> None:
        self.entries = entries

    def gcloud_json(self, *args: str) -> Any:
        if args[:2] == ("logging", "read"):
            return self.entries
        if args[:2] == ("builds", "list"):
            return [
                {
                    "id": "b1234567890",
                    "startTime": "2026-11-17T10:05:00Z",
                    "finishTime": "2026-11-17T10:35:00Z",
                }
            ]
        raise AssertionError(args)

    def access_token(self) -> str:
        return "not-a-token"


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    services = {name: name for name, _ in gcp_cost.SKUS.values()}
    (tmp_path / "services.json").write_text(json.dumps(services))
    skus = {
        "Kubernetes Engine": [sku("GKE", "Regional Kubernetes Clusters", "h", 0.1)],
        "Compute Engine": [
            sku("CORE", "E2 Instance Core running in Americas", "h", 0.02),
            sku("RAM", "E2 Instance Ram running in Americas", "GiBy.h", 0.003),
            sku("PD", "Balanced PD Capacity", "GiBy.mo", 0.1),
        ],
        "Cloud SQL": [
            sku(
                "CPU",
                "Cloud SQL for PostgreSQL: Regional - Enterprise Plus N vCPU in Iowa",
                "h",
                0.1,
            ),
            sku(
                "MEM",
                "Cloud SQL for PostgreSQL: Regional - Enterprise Plus N RAM in Iowa",
                "GiBy.h",
                0.02,
            ),
            sku(
                "SSD",
                "Cloud SQL for PostgreSQL: Regional - Enterprise Plus Standard Storage in Iowa",
                "GiBy.mo",
                0.34,
            ),
        ],
        "Cloud Logging": [sku("LOG", "Log Storage cost", "GiBy", 0.5, free=True)],
        "Cloud Build": [
            sku("CB-CPU", "E2 cpu utilization per minute in us-central1", "min", 0.0001),
            sku("CB-RAM", "E2 ram utilization per minute in us-central1", "GiBy.min", 0.00001),
        ],
    }
    for service, found in skus.items():
        (tmp_path / gcp_cost.cache_name(service)).write_text(json.dumps(found))
    return tmp_path


def test_each_resource_is_priced_for_its_life_in_the_audit_log(
    catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gcp_cost, "_log_bytes", lambda cloud, window: 2 * gcp_cost.GIB)
    monkeypatch.setattr(gcp_cost, "billing_export", lambda cloud, window: "none")
    cluster = "projects/p/locations/us-central1/clusters/iki-test"
    node = "projects/p/zones/us-central1-b/instances/gke-iki-test-system-abc-1234"
    sql = "projects/p/instances/iki-test"
    project = FakeProject(
        [
            entry(0, "google.container.v1.ClusterManager.CreateCluster", cluster),
            entry(9, "google.container.v1.ClusterManager.CreateCluster", cluster),
            entry(10, "v1.compute.instances.insert", node),
            entry(
                30,
                "cloudsql.instances.create",
                sql,
                {"body": {"settings": {"tier": "db-perf-optimized-N-2", "dataDiskSizeGb": "100"}}},
            ),
            entry(130, "v1.compute.instances.delete", node),
            entry(150, "cloudsql.instances.delete", sql),
            entry(170, "google.container.v1.ClusterManager.DeleteCluster", cluster),
            entry(180, "google.container.v1.ClusterManager.DeleteCluster", cluster),
        ]
    )
    window = gcp_cost.Window("p", "iki-test", "us-central1", START, END)
    variables = {"node_pools": {"system": {"machine_type": "e2-standard-4", "disk_size_gib": 50}}}
    result = gcp_cost.report(project, window, variables, {}, catalog)
    lines = {line.resource: line for line in result.lines}

    # From the first create entry to the last delete: 3 hours.
    assert lines["GKE cluster iki-test"].cost == pytest.approx(0.3)
    node_lines = [line for name, line in lines.items() if name.startswith("VM gke-iki-test")]
    # Two hours of an e2-standard-4: 4 cores, 16 GiB, and its 50 GiB boot disk.
    assert sum(line.cost for line in node_lines) == pytest.approx(
        2 * 4 * 0.02 + 2 * 16 * 0.003 + 2 * 50 * 0.1 / 730
    )
    sql_cost = sum(line.cost for name, line in lines.items() if name.startswith("Cloud SQL"))
    assert sql_cost == pytest.approx(2 * 2 * 0.1 + 2 * 16 * 0.02 + 2 * 100 * 0.34 / 730)
    # E2_HIGHCPU_8 for 30 minutes: 8 vCPUs' and 8 GiB's minutes.
    build = lines["Cloud Build b1234567 vCPUs"].cost + lines["Cloud Build b1234567 memory"].cost
    assert build == pytest.approx(8 * 30 * 0.0001 + 8 * 30 * 0.00001)
    # Logs at the rate beyond the free tier: the run can't know what else used it.
    assert lines["logs ingested (cluster and database)"].cost == pytest.approx(1.0)
    assert result.total == pytest.approx(sum(line.cost for line in result.lines))
    assert "total" in result.text()


def test_a_resource_without_a_price_is_shown_not_dropped(catalog: Path) -> None:
    window = gcp_cost.Window("p", "iki-test", "us-central1", START, END)
    project = FakeProject(
        [
            entry(
                0,
                "v1.compute.instances.insert",
                "projects/p/zones/z/instances/gke-iki-test-gpu-x",
                {"machineType": "zones/z/machineTypes/g2-standard-8"},
            ),
        ]
    )
    lives = gcp_cost.lifetimes(project, window)
    (life,) = lives.values()
    assert life.end is None and life.hours(window) == pytest.approx(3)
    assert gcp_cost._machine("g2-standard-8") is None
