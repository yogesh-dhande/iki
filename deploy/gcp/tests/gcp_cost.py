"""What a live test's deployment (test_gcp_live.py) cost, from what Google Cloud recorded.

Each billable resource's lifetime comes from the project's Admin Activity audit logs:
when it was created and deleted. That times the list price of its SKU, fetched from
the Cloud Billing Catalog API, in the deployment's region. Cloud Build's minutes come
from its builds, log volume from Cloud Monitoring, and Artifact Registry's storage from
the image sizes the test noted before tearing down, as Compute Engine's images' (the VM
tier's setup builds' versions). If the project exports billing to
BigQuery, the export's rows labelled ``iki-deployment=<name>`` are queried too, though they lag
by up to a day.

The test runs it when it has torn down. To run it again for a past run:

    IKI_GCP_PROJECT=<project> make gcp-live-cost

It reads the run's window (gcp-window.json) and facts (gcp-facts.json) from the
work directory, and writes gcp-cost.txt and gcp-cost.json there. List prices only:
not the billing account's discounts or free tiers, except Cloud Logging's, noted.
"""

import datetime
import json
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import live_cost
import requests
from live_cost import Life, Line

GIB = live_cost.GIB
_time = live_cost.parse_time


class Cloud(Protocol):
    """What the report needs of test_gcp_live.Gcp."""

    def gcloud_json(self, *args: str) -> Any: ...

    def access_token(self) -> str: ...


@dataclass
class Window:
    project: str
    name: str
    region: str
    start: datetime.datetime
    end: datetime.datetime

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


@dataclass
class Price:
    sku: str
    description: str
    unit: str
    usd: float  # per unit


@dataclass
class Report(live_cost.Report):
    billing_export: list[dict[str, Any]] | str = ""

    def heading(self) -> str:
        return (
            f"Cost of {self.window['name']} in {self.window['project']}, "
            f"{self.window['start']} to {self.window['end']} (list prices, USD)"
        )

    def footer(self) -> list[str]:
        if isinstance(self.billing_export, str):
            return [f"Billing export: {self.billing_export}"]
        return [
            "Billing export (labelled rows; lags up to a day):",
            *(
                f"  {row['service']}: {row['sku']} ({row['sku_id']}): "
                f"{row['usage']} {row['unit']}, {row['cost']} {row['currency']}"
                for row in self.billing_export
            ),
        ]


# --- Prices ----------------------------------------------------------------------------------

CATALOG = "https://cloudbilling.googleapis.com/v1"

#: What each resource is billed by: the service's display name in the catalog, and a
#: pattern its SKU's description matches. A SKU must also serve the deployment's region
#: (or be global).
SKUS: dict[str, tuple[str, str]] = {
    "gke_regional": ("Kubernetes Engine", r"^Regional Kubernetes Clusters$"),
    "e2_core": ("Compute Engine", r"^E2 Instance Core running in "),
    "e2_ram": ("Compute Engine", r"^E2 Instance Ram running in "),
    "pd_balanced": ("Compute Engine", r"^Balanced PD Capacity( in .*)?$"),
    "sql_vcpu": ("Cloud SQL", r"^Cloud SQL for PostgreSQL: Regional - Enterprise Plus N vCPU in "),
    "sql_ram": ("Cloud SQL", r"^Cloud SQL for PostgreSQL: Regional - Enterprise Plus N RAM in "),
    "sql_storage": (
        "Cloud SQL",
        r"^Cloud SQL for PostgreSQL: Regional - Enterprise Plus Standard Storage in ",
    ),
    "ilb_proxy": (
        "Networking",
        r"^Regional Internal Application Load Balancer Proxy Instance Charge for ",
    ),
    "nat_uptime": ("Networking", r"^Networking Cloud Nat Gateway Uptime$"),
    "nat_ip": ("Networking", r"^Networking Cloud NAT IP Usage$"),
    "dns_zone": ("Cloud DNS", r"^ManagedZone$"),
    "logs": ("Cloud Logging", r"^Log Storage cost$"),
    # Cloud Build bills a default pool's E2 machine by its vCPUs' and memory's minutes.
    "build_e2_cpu": ("Cloud Build", r"^E2 cpu utilization per minute in "),
    "build_e2_ram": ("Cloud Build", r"^E2 ram utilization per minute in "),
    "artifact_storage": ("Artifact Registry", r"(?i)^Artifact Registry Storage$"),
    # A setup build's version of a volume on the VM tier: an image, by its archive's size.
    "image": ("Compute Engine", r"^Storage Image( in .*)?$"),
    "kms_software": (
        "Cloud Key Management Service (KMS)",
        r"(?i)^Active software symmetric key versions$",
    ),
}


def cache_name(service: str) -> str:
    """The file a service's SKUs are kept in, in the catalog's cache."""
    return "skus-" + re.sub(r"\W+", "-", service) + ".json"


class Catalog:
    """The Cloud Billing Catalog's SKUs, fetched once per service and kept in cache."""

    def __init__(self, token: Callable[[], str], cache: Path) -> None:
        self.token = token
        self.cache = cache
        self.services: dict[str, str] | None = None
        self.skus: dict[str, list[dict[str, Any]]] = {}

    def _get(self, url: str, params: dict[str, str]) -> Iterable[dict[str, Any]]:
        token = ""
        while True:
            page = requests.get(
                url,
                params={**params, "pageSize": "5000", **({"pageToken": token} if token else {})},
                headers={"Authorization": f"Bearer {self.token()}"},
                allow_redirects=False,
                timeout=120,
            )
            if page.is_redirect:
                raise requests.HTTPError(
                    "The Cloud Billing Catalog redirected its request", response=page
                )
            page.raise_for_status()
            body: dict[str, Any] = page.json()
            yield body
            token = body.get("nextPageToken", "")
            if not token:
                return

    def service(self, name: str) -> str:
        if self.services is None:
            path = self.cache / "services.json"
            if path.exists():
                self.services = json.loads(path.read_text())
            else:
                self.services = {
                    s["displayName"]: s["serviceId"]
                    for page in self._get(f"{CATALOG}/services", {})
                    for s in page.get("services", [])
                }
                self.cache.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(self.services))
        return self.services[name]

    def of(self, service: str) -> list[dict[str, Any]]:
        if service not in self.skus:
            path = self.cache / cache_name(service)
            if path.exists():
                self.skus[service] = json.loads(path.read_text())
            else:
                found = [
                    sku
                    for page in self._get(
                        f"{CATALOG}/services/{self.service(service)}/skus", {"currencyCode": "USD"}
                    )
                    for sku in page.get("skus", [])
                ]
                self.cache.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(found))
                self.skus[service] = found
        return self.skus[service]

    def price(self, key: str, region: str) -> Price | None:
        service, pattern = SKUS[key]
        try:
            skus = self.of(service)
        except (KeyError, requests.RequestException):
            return None
        for sku in skus:
            regions = sku.get("serviceRegions", [])
            if not re.search(pattern, sku["description"]):
                continue
            if regions and region not in regions and "global" not in regions:
                continue
            if sku.get("category", {}).get("usageType", "OnDemand") != "OnDemand":
                continue
            expression = sku["pricingInfo"][0]["pricingExpression"]
            # The list rate: the dearest tier (a free tier's first, Cloud Logging's).
            usd = max(
                int(t["unitPrice"].get("units", "0")) + t["unitPrice"].get("nanos", 0) / 1e9
                for t in expression["tieredRates"]
            )
            return Price(sku["skuId"], sku["description"], expression["usageUnit"], usd)
        return None


# --- Lifetimes, from the audit logs -----------------------------------------------------------

METHODS = (
    r"(ClusterManager\.(Create|Delete)Cluster"
    r"|compute\.(instances|disks|images|forwardingRules|routers|addresses)\.(insert|delete)"
    r"|cloudsql\.instances\.(create|delete)"
    r"|ArtifactRegistry\.(Create|Delete)Repository"
    r"|CreateCryptoKey|DestroyCryptoKeyVersion"
    r"|storage\.buckets\.(create|delete)"
    r"|dns\.managedZones\.(create|delete))$"
)


def lifetimes(cloud: Cloud, window: Window) -> dict[str, Life]:
    """Each resource of the deployment's that's billed by the hour: its first create and
    its last delete entry (an operation logs a few), in the Admin Activity audit log."""
    until = window.end + datetime.timedelta(minutes=30)
    query = (
        f'logName="projects/{window.project}/logs/cloudaudit.googleapis.com%2Factivity"'
        f' AND timestamp>="{window.start.isoformat()}" AND timestamp<="{until.isoformat()}"'
        f' AND protoPayload.methodName=~"{METHODS}"'
        f' AND (protoPayload.resourceName:"{window.name}" OR protoPayload.resourceName:"gkegw1-")'
    )
    entries = (
        cloud.gcloud_json(
            "logging",
            "read",
            query,
            "--order=asc",
            "--limit=20000",
            "--format=json(timestamp,protoPayload.methodName,protoPayload.resourceName,protoPayload.request)",
        )
        or []
    )
    found: dict[str, Life] = {}
    for entry in entries:
        payload = entry.get("protoPayload", {})
        method, resource = payload.get("methodName", ""), payload.get("resourceName", "")
        kind = _kind(method)
        if kind == "kms":
            resource = resource.split("/cryptoKeyVersions/")[0]
        key = f"{kind}:{resource.rstrip('/')}"
        life = found.setdefault(key, Life(kind, resource.rsplit("/", 1)[-1]))
        made = bool(re.search(r"(Create|create|insert)", method.rsplit(".", 1)[-1]))
        life.saw(_time(entry["timestamp"]), made=made, request=payload.get("request"))
    return found


def _kind(method: str) -> str:
    for pattern, kind in (
        (r"ClusterManager", "cluster"),
        (r"compute\.instances", "instance"),
        (r"compute\.disks", "disk"),
        (r"compute\.images", "image"),
        (r"compute\.forwardingRules", "forwarding rule"),
        (r"compute\.routers", "router"),
        (r"compute\.addresses", "address"),
        (r"cloudsql", "sql"),
        (r"ArtifactRegistry", "repository"),
        (r"CryptoKey", "kms"),
        (r"storage\.buckets", "bucket"),
        (r"dns\.managedZones", "dns zone"),
    ):
        if re.search(pattern, method):
            return kind
    return "other"


# --- The report -------------------------------------------------------------------------------


#: The shared-core E2 machines (the VM tier's light stand-ins): the fraction of a vCPU
#: each is billed for, and its GiB of memory.
SHARED_CORE = {"e2-micro": (0.25, 1.0), "e2-small": (0.5, 2.0), "e2-medium": (1.0, 4.0)}


def _machine(machine: str) -> tuple[float, float] | None:
    """An E2 machine type's vCPUs and GiB of memory."""
    if machine in SHARED_CORE:
        return SHARED_CORE[machine]
    match = re.fullmatch(r"e2-(standard|highcpu|highmem)-(\d+)", machine)
    if not match:
        return None
    cpus = int(match.group(2))
    return cpus, cpus * {"standard": 4, "highcpu": 1, "highmem": 8}[match.group(1)]


def _disk_sizes(request: Any) -> list[float]:
    """The GiB of each disk an instances.insert request makes with its instance: none
    when the request doesn't say (a node's, which its pool gives)."""
    disks = request.get("disks") if isinstance(request, dict) else None
    return [
        float(d["initializeParams"]["diskSizeGb"])
        for d in disks or []
        if isinstance(d, dict)
        and str(d.get("initializeParams", {}).get("diskSizeGb", "")).isdigit()
    ]


def _sql_tier(tier: str) -> tuple[float, float] | None:
    """An Enterprise Plus N tier's vCPUs and GiB of memory (8 per vCPU)."""
    match = re.fullmatch(r"db-perf-optimized-N-(\d+)", tier)
    return (int(match.group(1)), int(match.group(1)) * 8) if match else None


def _per_hour(price: Price, quantity_hours: float) -> float:
    """The cost of quantity_hours of a unit-hour (h, GiBy.h) or unit-month SKU."""
    return live_cost.prorate(price.usd, price.unit, quantity_hours)


def report(
    cloud: Cloud, window: Window, variables: dict[str, Any], facts: dict[str, Any], cache: Path
) -> Report:
    catalog = Catalog(cloud.access_token, cache)
    result = Report(
        window={
            "project": window.project,
            "name": window.name,
            "start": window.start.isoformat(timespec="seconds"),
            "end": window.end.isoformat(timespec="seconds"),
        }
    )

    def add(
        resource: str, key: str, hours: float, quantity: float, unit: str, note: str = ""
    ) -> None:
        price = catalog.price(key, window.region)
        if price is None:
            result.lines.append(
                Line(resource, hours, quantity, unit, "?", f"no SKU for {key}", 0, 0, note)
            )
            return
        units = quantity * hours if unit.endswith("h") else quantity
        cost = _per_hour(price, units) if unit.endswith("h") else price.usd * units
        result.lines.append(
            Line(
                resource, hours, quantity, unit, price.sku, price.description, price.usd, cost, note
            )
        )

    lives = lifetimes(cloud, window)
    pools = variables.get("node_pools") or {}
    vm_hours = 0.0
    for life in sorted(lives.values(), key=lambda life: (life.kind, life.name)):
        hours = life.hours(window)
        open_note = "" if life.end else "no delete in the audit log: until the window's end"
        if life.kind == "cluster":
            add(f"GKE cluster {life.name}", "gke_regional", hours, 1, "h", open_note)
        elif life.kind == "instance":
            vm_hours += hours
            pool = next((p for p in pools if life.name.startswith(f"gke-{window.name}-{p}-")), None)
            machine = str(life.request.get("machineType", "")).rsplit("/", 1)[-1]
            config = pools.get(pool, {}) if pool else {}
            machine = machine or config.get("machine_type", "")
            shape = _machine(machine)
            if shape is None:
                result.lines.append(
                    Line(
                        f"VM {life.name}",
                        hours,
                        0,
                        "h",
                        "?",
                        f"no price for {machine or 'its machine'}",
                        0,
                        0,
                    )
                )
                continue
            add(f"VM {life.name} ({machine}) cores", "e2_core", hours, shape[0], "h", open_note)
            add(f"VM {life.name} ({machine}) memory", "e2_ram", hours, shape[1], "GiBy.h")
            # A VM-tier machine's request gives its disks' sizes; a node's, its pool's.
            sizes = _disk_sizes(life.request)
            if sizes:
                add(
                    f"VM {life.name} disks",
                    "pd_balanced",
                    hours,
                    sum(sizes),
                    "GiBy.h",
                    f"pd-balanced, {len(sizes)} as its request made them",
                )
                continue
            disk = float(config.get("disk_size_gib", 100))
            add(
                f"VM {life.name} boot disk",
                "pd_balanced",
                hours,
                disk,
                "GiBy.h",
                "pd-balanced, the pool's size",
            )
        elif life.kind == "disk":
            size = float(life.request.get("sizeGb", 0) or 0)
            add(
                f"disk {life.name}",
                "pd_balanced",
                hours,
                size,
                "GiBy.h",
                open_note or "assumed pd-balanced",
            )
        elif life.kind == "image":
            size = float(facts.get("image_bytes", {}).get(life.name, 0)) / GIB
            add(
                f"image {life.name}",
                "image",
                hours,
                size,
                "GiBy.h",
                open_note or "its archive's size, billed per month; prorated here",
            )
        elif life.kind == "sql":
            body = life.request.get("body", life.request)
            settings = body.get("settings", {}) if isinstance(body, dict) else {}
            tier = settings.get("tier") or variables.get("database_tier", "")
            shape = _sql_tier(tier)
            disk = float(settings.get("dataDiskSizeGb") or 100)
            if shape:
                add(
                    f"Cloud SQL {life.name} ({tier}) vCPUs",
                    "sql_vcpu",
                    hours,
                    shape[0],
                    "h",
                    open_note,
                )
                add(f"Cloud SQL {life.name} ({tier}) memory", "sql_ram", hours, shape[1], "GiBy.h")
            add(f"Cloud SQL {life.name} storage", "sql_storage", hours, disk, "GiBy.h")
        elif life.kind == "forwarding rule" and life.name.startswith("gkegw1-"):
            add(
                f"internal load balancer {life.name}",
                "ilb_proxy",
                hours,
                1,
                "h",
                "one proxy instance: Google may run more, by traffic",
            )
        elif life.kind == "router":
            pass  # The NAT gateway's charge is per VM: below.
        elif life.kind == "address" and "-nat-" in life.name:
            add(f"NAT address {life.name}", "nat_ip", hours, 1, "h", open_note)
        elif life.kind == "dns zone":
            add(
                f"DNS zone {life.name}",
                "dns_zone",
                hours,
                1,
                "h",
                "billed per month; prorated here",
            )
        elif life.kind == "kms":
            add(
                f"KMS key {life.name.rsplit('/', 1)[-1]}",
                "kms_software",
                hours,
                1,
                "h",
                "a version billed per month while enabled; prorated here",
            )
    if vm_hours:
        add("Cloud NAT gateway (per VM)", "nat_uptime", vm_hours, 1, "h", "VM-hours behind NAT")
    for life in lives.values():
        if life.kind == "repository":
            size = float(facts.get("artifact_bytes", {}).get(life.name, 0)) / GIB
            add(
                f"Artifact Registry {life.name}",
                "artifact_storage",
                life.hours(window),
                size,
                "GiBy.h",
            )
    for build in facts.get("builds", []) or _builds(cloud, window):
        minutes = (_time(build["finishTime"]) - _time(build["startTime"])).total_seconds() / 60
        # E2_HIGHCPU_8: 8 vCPUs, 8 GiB.
        name = f"Cloud Build {build['id'][:8]}"
        add(f"{name} vCPUs", "build_e2_cpu", minutes / 60, 8 * minutes, "min", "E2_HIGHCPU_8")
        add(f"{name} memory", "build_e2_ram", minutes / 60, 8 * minutes, "GiBy.min")
    logged = _log_bytes(cloud, window) / GIB
    add(
        "logs ingested (cluster and database)",
        "logs",
        window.hours,
        logged,
        "GiBy",
        "the project's first 50 GiB a month are free, shared with its other workloads",
    )
    result.billing_export = billing_export(cloud, window)
    return result


def _builds(cloud: Cloud, window: Window) -> list[dict[str, Any]]:
    builds = (
        cloud.gcloud_json(
            "builds",
            "list",
            f"--filter=tags={window.name} AND createTime>={window.start.isoformat()}",
            "--format=json(id,startTime,finishTime,options.machineType)",
        )
        or []
    )
    return [b for b in builds if b.get("startTime") and b.get("finishTime")]


def _log_bytes(cloud: Cloud, window: Window) -> float:
    """Bytes of logs the cluster's and the database's resources sent Cloud Logging."""
    total = 0.0
    for selector in (
        f'resource.labels.cluster_name="{window.name}"',
        'resource.type="cloudsql_database"'
        f' AND resource.labels.database_id="{window.project}:{window.name}"',
    ):
        response = requests.get(
            f"https://monitoring.googleapis.com/v3/projects/{window.project}/timeSeries",
            params={
                "filter": f'metric.type="logging.googleapis.com/byte_count" AND {selector}',
                "interval.startTime": window.start.isoformat(),
                "interval.endTime": (window.end + datetime.timedelta(minutes=10)).isoformat(),
                "aggregation.alignmentPeriod": f"{int(window.hours * 3600) + 600}s",
                "aggregation.perSeriesAligner": "ALIGN_SUM",
                "aggregation.crossSeriesReducer": "REDUCE_SUM",
            },
            headers={"Authorization": f"Bearer {cloud.access_token()}"},
            allow_redirects=False,
            timeout=120,
        )
        if response.status_code != 200:
            continue
        for series in response.json().get("timeSeries", []):
            for point in series.get("points", []):
                value = point["value"]
                total += float(value.get("int64Value", value.get("doubleValue", 0)))
    return total


def billing_export(cloud: Cloud, window: Window) -> list[dict[str, Any]] | str:
    """The project's billing export's rows labelled iki-deployment=<name>, if it has one."""
    headers = {"Authorization": f"Bearer {cloud.access_token()}"}
    api = f"https://bigquery.googleapis.com/bigquery/v2/projects/{window.project}"
    datasets = requests.get(f"{api}/datasets", headers=headers, allow_redirects=False, timeout=60)
    if datasets.status_code != 200:
        return f"couldn't list BigQuery datasets (HTTP {datasets.status_code})"
    tables = []
    for dataset in datasets.json().get("datasets", []):
        ref = dataset["datasetReference"]["datasetId"]
        listed = requests.get(
            f"{api}/datasets/{ref}/tables", headers=headers, allow_redirects=False, timeout=60
        )
        for table in listed.json().get("tables", []) if listed.status_code == 200 else []:
            name = table["tableReference"]["tableId"]
            if name.startswith(("gcp_billing_export_v1_", "gcp_billing_export_resource_v1_")):
                tables.append(f"{window.project}.{ref}.{name}")
    if not tables:
        return f"none: no gcp_billing_export_* table in any of {window.project}'s datasets"
    rows: list[dict[str, Any]] = []
    for table in tables[:1]:
        # The table's name is the API's; the values are the query's parameters.
        sql = (
            "SELECT service.description AS service, sku.description AS sku, sku.id AS sku_id,"  # noqa: S608
            " SUM(usage.amount_in_pricing_units) AS usage, ANY_VALUE(usage.pricing_unit) AS unit,"
            " SUM(cost) AS cost, ANY_VALUE(currency) AS currency"
            f" FROM `{table}`"
            " WHERE usage_start_time >= @start AND usage_end_time <= @end"
            " AND EXISTS (SELECT 1 FROM UNNEST(labels) l"
            " WHERE l.key = 'iki-deployment' AND l.value = @label)"
            " GROUP BY 1, 2, 3 ORDER BY cost DESC"
        )
        end = window.end + datetime.timedelta(hours=2)
        params = [
            _parameter("start", "TIMESTAMP", window.start.isoformat()),
            _parameter("end", "TIMESTAMP", end.isoformat()),
            _parameter("label", "STRING", window.name),
        ]
        answer = requests.post(
            f"{api}/queries",
            headers=headers,
            allow_redirects=False,
            json={
                "query": sql,
                "useLegacySql": False,
                "parameterMode": "NAMED",
                "queryParameters": params,
                "timeoutMs": 60000,
            },
            timeout=120,
        )
        if answer.status_code != 200:
            return f"{table}: the query failed (HTTP {answer.status_code})"
        fields = [f["name"] for f in answer.json().get("schema", {}).get("fields", [])]
        rows += [
            dict(zip(fields, (c["v"] for c in r["f"]), strict=True))
            for r in answer.json().get("rows", [])
        ]
        if not rows:
            return (
                f"{table}: no rows labelled iki-deployment={window.name} yet (it lags up to a day)"
            )
    return rows


def _parameter(name: str, kind: str, value: str) -> dict[str, Any]:
    return {"name": name, "parameterType": {"type": kind}, "parameterValue": {"value": value}}


def write(result: Report, workdir: Path) -> str:
    return live_cost.write(result, workdir, "gcp")


def main() -> None:
    """Reports a past run's cost, as its environment named it (test_gcp_live.Settings)."""
    import test_gcp_live as live

    settings = live.Settings.from_env()
    saved = json.loads((settings.workdir / "gcp-window.json").read_text())
    window = Window(
        settings.project,
        settings.name,
        settings.region,
        _time(saved["start"]),
        _time(saved["end"]) if saved.get("end") else datetime.datetime.now(datetime.UTC),
    )
    gcp = live.Gcp(settings)
    gcp.token.start()
    try:
        variables = json.loads(
            (settings.workdir / "deploy/gcp/production/terraform.tfvars.json").read_text()
        )
        facts_path = settings.workdir / "gcp-facts.json"
        facts = json.loads(facts_path.read_text()) if facts_path.exists() else {}
        sys.stdout.write(
            write(
                report(gcp, window, variables, facts, settings.workdir / "skus"), settings.workdir
            )
        )
    finally:
        gcp.stop()


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    main()
