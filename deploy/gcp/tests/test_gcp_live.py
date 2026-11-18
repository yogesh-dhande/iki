"""Iki deployed for real on Google Cloud, checked end to end, then torn down.

    IKI_GCP_PROJECT=<project> make check-gcp-live

It follows deploy/gcp/production/README.md's runbook in IKI_GCP_PROJECT: the bootstrap,
the infrastructure (made small: two system nodes, no GPUs, the smallest database, no DR
replica), External Secrets, the secrets and database roles, and Iki's chart, with server
and worker images Cloud Build makes there. Iki is served by an internal load balancer
only, with a certificate this test makes; the test reaches it by `kubectl port-forward`.
Then it checks what a deployment must do, and always tears it down with
deploy/gcp/scripts/teardown.sh, even when setting up or a check failed. Last, it lists
the project again and fails if anything of the deployment's is left but what Google
Cloud won't delete (key rings, keys with their versions scheduled for destruction, the
log bucket and custom roles pending deletion) or an API it turned on. A run that passes
whole, of a clean tree, is recorded in deploy/live-runs.json (deploy/live/live_runs.py):
commit it.

It costs: a regional GKE cluster's fee and three or four small nodes, a highly
available Cloud SQL Enterprise Plus instance (2 vCPUs), Cloud NAT, an internal load
balancer and two Cloud Build builds: a few US dollars for its hour or two. It never runs
on CI (conftest.py). What it shares with the AWS one is deploy/live/iki_live.py.

Who it acts as is one OAuth access token, for gcloud, OpenTofu, kubectl and Helm alike
(``Gcp`` below): IKI_GCP_TOKEN_COMMAND prints one, and is run again every 20 minutes. By
default it's ``gcloud auth print-access-token``, as your gcloud account; to act as a
service account, add ``--impersonate-service-account=<email>``, or give any command
whose last line is a token. Your gcloud configuration and credentials are otherwise
unused and unchanged. Other settings:

- IKI_GCP_NAME: the deployment's name; by default each run gets its own,
  ``iki-test-<MMDDHHMM>`` (UTC), since a name can't be used again in a project: Cloud KMS
  key rings are never deleted, and custom IAM roles' IDs are kept for 37 days. Everything
  it makes is named for it, and labelled ``iki-test=true`` and ``iki-deployment=<name>``.
- IKI_GCP_REGION, IKI_GCP_DR_REGION, IKI_GCP_MULTI_REGION, IKI_GCP_ZONES (comma
  separated): us-central1, us-east1, us, and the region's b and c zones by default.
- IKI_GCP_DATA_ACCESS: the services whose Data Access audit logs it turns on, comma
  separated. Not Cloud Storage's by default: in a shared project they'd log every other
  workload's reads. It refuses to start if any already has an audit config, which
  applying would replace and destroying would remove.
- IKI_GCP_WORKDIR: where it keeps OpenTofu's state, the kubeconfig and the certificate,
  ``build/gcp-live/<name>`` by default. If a run is killed before tearing down, run
  ``IKI_TEARDOWN_YES=1 deploy/gcp/scripts/teardown.sh <project> <name> --bootstrap``
  (build/gcp-live/last-run holds the last run's name)
  from there.
"""

import datetime
import ipaddress
import json
import os
import re
import shutil
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import gcp_cost
import iki_live
import pytest
import requests
import yaml
from iki_live import REPO, Iki, until

pytestmark = pytest.mark.gcp_live

#: Bumped when what the suite checks changes: recorded with each passing run.
SUITE_VERSION = "1"
#: Services whose Data Access audit logs are on in a project of Iki's own (the logs
#: module's default), less Cloud Storage's: a shared project's other workloads read there.
# Cloud SQL's audit logs are cloudsql.googleapis.com's (sqladmin has no audit config).
DATA_ACCESS = ("secretmanager.googleapis.com", "cloudkms.googleapis.com", "cloudsql.googleapis.com")
#: Credentials the caller's environment might hold, which the tools mustn't use instead of
#: the token (Gcp).
_CREDENTIAL_VARIABLES = {
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CREDENTIALS",
    "GOOGLE_OAUTH_ACCESS_TOKEN",
    "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
    "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT",
    "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
    "CLOUDSDK_CONFIG",
    "KUBECONFIG",
}


# --- Settings ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    project: str
    name: str
    region: str
    dr_region: str
    multi_region: str
    zones: tuple[str, ...]
    data_access: tuple[str, ...]
    token_command: str
    workdir: Path

    @classmethod
    def from_env(cls, *, new: bool = False) -> "Settings":
        """The run's settings. Unless IKI_GCP_NAME names it, a new run (new) gets a name of
        its own (iki_live.run_name), and a later look (the cost report) takes the last."""
        env = os.environ
        name = iki_live.run_name("gcp", env.get("IKI_GCP_NAME", ""), new=new)
        region = env.get("IKI_GCP_REGION", "us-central1")
        zones = env.get("IKI_GCP_ZONES", f"{region}-b,{region}-c")
        return cls(
            project=env["IKI_GCP_PROJECT"],
            name=name,
            region=region,
            dr_region=env.get("IKI_GCP_DR_REGION", "us-east1"),
            multi_region=env.get("IKI_GCP_MULTI_REGION", "us"),
            zones=tuple(z.strip() for z in zones.split(",") if z.strip()),
            data_access=tuple(
                s.strip() for s in env.get("IKI_GCP_DATA_ACCESS", ",".join(DATA_ACCESS)).split(",")
            ),
            token_command=env.get("IKI_GCP_TOKEN_COMMAND", "gcloud auth print-access-token"),
            workdir=Path(env.get("IKI_GCP_WORKDIR", REPO / "build" / "gcp-live" / name)).resolve(),
        )

    @property
    def host(self) -> str:
        """public_host: a name only the deployment's VPC resolves (its private zone)."""
        return f"{self.name}.internal"

    @property
    def certificate(self) -> str:
        return f"{self.name}-internal"


# --- The one place gcloud, OpenTofu, kubectl and Helm run ------------------------------------


class Gcp(iki_live.Tools):
    """The tools in the work directory's copy of deploy/gcp, all as one identity: an OAuth
    access token from IKI_GCP_TOKEN_COMMAND (iki_live.Token). gcloud reads it from its file
    (CLOUDSDK_AUTH_ACCESS_TOKEN_FILE) with a gcloud configuration of its own, so the
    caller's accounts and settings are never used or changed; kubectl from the same file
    (the kubeconfig's tokenFile); OpenTofu from GOOGLE_OAUTH_ACCESS_TOKEN, set fresh for
    each run."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        token = iki_live.Token(
            settings.token_command, "IKI_GCP_TOKEN_COMMAND", prefix="iki-gcp-live-"
        )
        super().__init__(
            settings.workdir,
            token,
            {
                "CLOUDSDK_CONFIG": str(token.dir / "gcloud"),
                "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE": str(token.path),
                "CLOUDSDK_CORE_PROJECT": settings.project,
                "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
            },
            _CREDENTIAL_VARIABLES,
        )

    @property
    def token_file(self) -> Path:
        return self.token.path

    def extra_env(self) -> dict[str, str]:
        return {"GOOGLE_OAUTH_ACCESS_TOKEN": self.token.path.read_text()}

    def access_token(self) -> str:
        """The current token, for a REST call of the test's own (gcp_cost.py)."""
        return self.token.read()

    def identity(self) -> str:
        """Whom the token is for: Google's token info, the token in the body, not the URL."""
        info = requests.post(
            "https://oauth2.googleapis.com/tokeninfo",
            data={"access_token": self.access_token()},
            timeout=60,
        )
        assert info.status_code == 200, f"the token's info: HTTP {info.status_code}"
        email: str = info.json().get("email", "")
        return email

    def gcloud(self, *args: str, **kwargs: Any) -> str:
        return self.run("gcloud", *args, "--project", self.settings.project, **kwargs).stdout

    def gcloud_json(self, *args: str) -> Any:
        return json.loads(self.gcloud(*args, "--format=json") or "null")

    def tofu(self, root: str, *args: str, log: str = "tofu.log", timeout: float = 3000) -> None:
        # A fresh token: OpenTofu reads it once, and no run here outlasts its hour.
        self.mint()
        self.stream("tofu", f"-chdir=deploy/gcp/{root}", *args, log=log, timeout=timeout)

    def output(self, name: str) -> str:
        return self.run("tofu", "-chdir=deploy/gcp/production", "output", "-raw", name).stdout


# --- What's in the project, before and after -----------------------------------------------

#: Each section of the inventory: a gcloud listing, one line per resource.
LISTINGS: dict[str, tuple[str, ...]] = {
    "instances": ("compute", "instances", "list", "--format=value(name,zone.basename())"),
    "disks": ("compute", "disks", "list", "--format=value(name,zone.basename())"),
    "images": ("compute", "images", "list", "--no-standard-images", "--format=value(name)"),
    "snapshots": ("compute", "snapshots", "list", "--format=value(name)"),
    "instance templates": ("compute", "instance-templates", "list", "--format=value(name)"),
    "networks": ("compute", "networks", "list", "--format=value(name)"),
    "subnets": ("compute", "networks", "subnets", "list", "--format=value(name,region.basename())"),
    "firewalls": ("compute", "firewall-rules", "list", "--format=value(name,network.basename())"),
    "routers": ("compute", "routers", "list", "--format=value(name,region.basename())"),
    "addresses": ("compute", "addresses", "list", "--format=value(name,region.basename())"),
    "ssl certificates": ("compute", "ssl-certificates", "list", "--format=value(name)"),
    "ssl policies": ("compute", "ssl-policies", "list", "--format=value(name)"),
    "security policies": ("compute", "security-policies", "list", "--format=value(name)"),
    "forwarding rules": ("compute", "forwarding-rules", "list", "--format=value(name)"),
    "backend services": ("compute", "backend-services", "list", "--format=value(name)"),
    "health checks": ("compute", "health-checks", "list", "--format=value(name)"),
    "url maps": ("compute", "url-maps", "list", "--format=value(name)"),
    "target https proxies": ("compute", "target-https-proxies", "list", "--format=value(name)"),
    "network endpoint groups": (
        "compute",
        "network-endpoint-groups",
        "list",
        "--format=value(name)",
    ),
    "clusters": ("container", "clusters", "list", "--format=value(name,location)"),
    "sql instances": ("sql", "instances", "list", "--format=value(name,state)"),
    "sql backups": ("sql", "backups", "list", "--instance=-", "--format=value(instance,status)"),
    "buckets": ("storage", "buckets", "list", "--format=value(name)"),
    "secrets": ("secrets", "list", "--format=value(name)"),
    "service accounts": ("iam", "service-accounts", "list", "--format=value(email)"),
    "custom roles": ("iam", "roles", "list", "--show-deleted", "--format=value(name,deleted)"),
    "artifact registry": ("artifacts", "repositories", "list", "--format=value(name)"),
    "log sinks": ("logging", "sinks", "list", "--format=value(name)"),
    "log buckets": ("logging", "buckets", "list", "--format=value(name,lifecycleState)"),
    "dns zones": ("dns", "managed-zones", "list", "--format=value(name,dnsName)"),
    "apis": ("services", "list", "--enabled", "--format=value(config.name)"),
}


@dataclass
class Inventory:
    listings: dict[str, set[str]]
    key_versions: dict[str, set[str]]
    bindings: set[tuple[str, str]]
    audit_configs: list[Any]
    binauthz: Any

    def write(self, path: Path) -> None:
        sections = dict(self.listings)
        for key, versions in sorted(self.key_versions.items()):
            sections[f"kms key versions of {key}"] = versions
        sections["project IAM (role member)"] = {" ".join(b) for b in self.bindings}
        tail = ["=== audit configs", json.dumps(self.audit_configs, indent=1, sort_keys=True)]
        tail += ["", "=== binary authorization policy", json.dumps(self.binauthz, sort_keys=True)]
        iki_live.write_sections(path, sections, tail)


def inventory(gcp: Gcp, rings: list[str]) -> Inventory:
    """Everything in the project the test could leave behind. A listing whose API is off
    is empty."""
    listings = {}
    for section, args in LISTINGS.items():
        result = gcp.run("gcloud", *args, "--project", gcp.settings.project, check=False)
        listings[section] = {line for line in result.stdout.splitlines() if line.strip()}
    key_versions = {}
    for ring in rings:
        keys = gcp.run(
            "gcloud", "kms", "keys", "list", "--keyring", ring, "--format=value(name)", check=False
        )
        for key in keys.stdout.split():
            versions = gcp.run(
                "gcloud",
                "kms",
                "keys",
                "versions",
                "list",
                "--key",
                key,
                "--format=value(name.basename(),state)",
                check=False,
            )
            key_versions[key] = set(versions.stdout.splitlines())
    policy = gcp.gcloud_json("projects", "get-iam-policy", gcp.settings.project)
    bindings = {(b["role"], m) for b in policy.get("bindings", []) for m in b["members"]}
    binauthz = gcp.run(
        "gcloud",
        "container",
        "binauthz",
        "policy",
        "export",
        "--project",
        gcp.settings.project,
        "--format=json",
        check=False,
    )
    return Inventory(
        listings=listings,
        key_versions=key_versions,
        bindings=bindings,
        audit_configs=policy.get("auditConfigs", []),
        binauthz=json.loads(binauthz.stdout) if binauthz.returncode == 0 else None,
    )


#: Cloud KMS's names for Cloud Storage's predefined dual-regions (deploy/gcp/production).
PREDEFINED_DUAL_REGIONS = ("asia1", "eur4", "eur5", "eur7", "eur8", "nam4")


def key_rings(gcp: Gcp, s: Settings) -> list[str]:
    """The deployment's key rings, where it makes them: the files key is in a predefined
    dual-region (nam4) when its two regions make one."""
    found = []
    for location in sorted({s.region, s.dr_region, s.multi_region, *PREDEFINED_DUAL_REGIONS}):
        listed = gcp.run(
            "gcloud",
            "kms",
            "keyrings",
            "list",
            "--location",
            location,
            "--project",
            s.project,
            "--format=value(name)",
            check=False,
        )
        found += [r for r in listed.stdout.split() if r.split("/")[-1].startswith(s.name)]
    return found


#: Names GKE gives what it makes for a cluster and its Gateway: they don't all carry the
#: deployment's name.
GKE_MADE = re.compile(r"^(gke-|k8s1-|k8s2-|gkegw1-|gkemcg1-)")


def leftovers(before: Inventory, after: Inventory, s: Settings) -> list[str]:
    """What the deployment left that it may not: empty if only what Google Cloud keeps."""
    ours = (s.name, s.name.replace("-", "_"))
    problems = []
    for section, found in after.listings.items():
        new = found - before.listings.get(section, set())
        if section == "apis":
            gone = before.listings[section] - found
            problems += [f"API turned off: {api}" for api in sorted(gone)]
            continue
        for line in sorted(new):
            # Cloud SQL's zone for its names under Private Services Access, made for
            # the deployment's instance and named for nothing of it.
            psa = section == "dns zones" and line.startswith("cloud-sql-psa-dns-")
            mine = any(o in line for o in ours) or bool(GKE_MADE.match(line)) or psa
            if not mine:
                continue
            # Deleted, and waiting out the period Google keeps their names for.
            if section == "custom roles" and line.endswith("True"):
                continue
            if section == "log buckets" and "DELETE_REQUESTED" in line:
                continue
            problems.append(f"{section}: {line}")
    if f"{s.project}_cloudbuild" in after.listings["buckets"] - before.listings["buckets"]:
        problems.append(f"buckets: {s.project}_cloudbuild, which Cloud Build made for this test")
    for key, versions in after.key_versions.items():
        for line in versions:
            if not line.endswith(("DESTROY_SCHEDULED", "DESTROYED")):
                problems.append(f"{key}: version {line}, not scheduled for destruction")
    for role, member in sorted(before.bindings - after.bindings):
        problems.append(f"IAM: {member} lost {role}, which it had before")
    for role, member in sorted(after.bindings - before.bindings):
        if any(o in role or o in member for o in ours):
            problems.append(f"IAM: {member} still has {role}")
    if after.audit_configs != before.audit_configs:
        problems.append(f"audit configs changed: {before.audit_configs} -> {after.audit_configs}")
    policy = after.binauthz or {}
    patterns = [p.get("namePattern", "") for p in policy.get("admissionWhitelistPatterns", [])]
    if before.binauthz != after.binauthz and (
        any(s.name in p for p in patterns)
        or policy.get("defaultAdmissionRule", {}).get("evaluationMode", "ALWAYS_ALLOW")
        != "ALWAYS_ALLOW"
    ):
        problems.append(f"the Binary Authorization policy is still the deployment's: {policy}")
    return problems


# --- The deployment --------------------------------------------------------------------------


@dataclass
class Deployment(iki_live.Live):
    settings: Settings = None  # type: ignore[assignment]

    @property
    def gcp(self) -> Gcp:
        assert isinstance(self.tools, Gcp)
        return self.tools

    def inventory(self) -> Inventory:
        return inventory(self.gcp, key_rings(self.gcp, self.settings))

    def preconditions(self) -> None:
        check_preconditions(self.gcp, self.settings, self.before)

    def stand_up(self) -> None:
        stand_up(self)

    def diagnose(self) -> None:
        iki_live.diagnose(self.gcp, "mirror.gcr.io/library/postgres:17")

    def leftovers(self, before: Inventory, after: Inventory) -> list[str]:
        return leftovers(before, after, self.settings)

    def cost_report(self, start: datetime.datetime) -> str:
        return cost_report(self, start)

    def teardown_steps(self) -> list[tuple[str, Callable[[], object]]]:
        s, gcp = self.settings, self.gcp
        steps: list[tuple[str, Callable[[], object]]] = [
            ("the facts the cost report needs", lambda: record_facts(gcp, s)),
            ("teardown.sh", lambda: run_teardown(gcp, s)),
            (
                "the internal certificate",
                lambda: gcp.run(
                    "gcloud",
                    "compute",
                    "ssl-certificates",
                    "delete",
                    s.certificate,
                    "--region",
                    s.region,
                    "--project",
                    s.project,
                    "--quiet",
                    check=False,
                ),
            ),
            (
                "Cloud Build's source",
                lambda: gcp.run(
                    "gcloud",
                    "storage",
                    "rm",
                    "--recursive",
                    f"gs://{s.project}_cloudbuild/{s.name}-live",
                    check=False,
                ),
            ),
        ]
        if self.before and f"{s.project}_cloudbuild" not in self.before.listings["buckets"]:
            steps.append(
                (
                    "Cloud Build's bucket",
                    lambda: gcp.run(
                        "gcloud",
                        "storage",
                        "rm",
                        "--recursive",
                        f"gs://{s.project}_cloudbuild",
                        check=False,
                    ),
                )
            )
        return steps


def run_teardown(gcp: Gcp, s: Settings) -> None:
    """teardown.sh, and once more if it fails: it skips what's gone, so a second run
    finishes what a passing failure (an API's hiccup, a lock) stopped."""
    for attempt in (1, 2):
        try:
            gcp.mint()
            gcp.stream(
                "deploy/gcp/scripts/teardown.sh",
                s.project,
                s.name,
                "--bootstrap",
                log="teardown.log",
                timeout=5400,
            )
            return
        except AssertionError:
            if attempt == 2:
                raise


def record_facts(gcp: Gcp, s: Settings) -> None:
    """What the cost report can't learn once the deployment is gone: its images' sizes."""
    facts: dict[str, Any] = {"artifact_bytes": {}}
    listed = gcp.run(
        "tofu", "-chdir=deploy/gcp/production", "output", "-json", "images", check=False
    )
    for url in json.loads(listed.stdout).values() if listed.returncode == 0 else []:
        images = gcp.gcloud_json("artifacts", "docker", "images", "list", url, "--include-tags")
        # gcloud gives sizes as strings, and "None" for a multi-platform index.
        sizes = (
            str(i.get("metadata", {}).get("imageSizeBytes") or i.get("sizeBytes") or "")
            for i in images or []
        )
        facts["artifact_bytes"][url.rsplit("/", 1)[-1]] = sum(
            int(size) for size in sizes if size.isdigit()
        )
    (s.workdir / "gcp-facts.json").write_text(json.dumps(facts, indent=1))


def cost_report(d: Deployment, start: datetime.datetime) -> str:
    """What the run cost, at list prices (gcp_cost.py), in the work directory."""
    s, gcp = d.settings, d.gcp
    end = datetime.datetime.now(datetime.UTC)
    window_file = s.workdir / "gcp-window.json"
    window_file.write_text(json.dumps({"start": start.isoformat(), "end": end.isoformat()}))
    tfvars = s.workdir / "deploy/gcp/production/terraform.tfvars.json"
    variables = json.loads(tfvars.read_text()) if tfvars.exists() else {}
    facts_file = s.workdir / "gcp-facts.json"
    facts = json.loads(facts_file.read_text()) if facts_file.exists() else {}
    window = gcp_cost.Window(s.project, s.name, s.region, start, end)
    result = gcp_cost.report(gcp, window, variables, facts, s.workdir / "skus")
    d.cost_usd = result.total
    return gcp_cost.write(result, s.workdir)


def check_preconditions(gcp: Gcp, s: Settings, before: Inventory) -> None:
    """Refuses to start where applying would change what isn't the test's."""
    for name in ("gcloud", "tofu", "kubectl", "helm", "jq", "curl", "openssl", "uv"):
        iki_live.tool(name)
    gcp.gcloud("projects", "describe", s.project, "--format=value(projectId)")
    # The identity it runs as may be named for the deployment too (iki-test-deployer@...).
    me = gcp.identity()
    taken = [
        line
        for found in before.listings.values()
        for line in found
        if line.startswith(f"{s.name}-") and not (me and line.startswith(me))
    ]
    assert not taken, f"{s.name}'s resources already exist; tear them down first: {taken[:10]}"
    configured = {c["service"] for c in before.audit_configs}
    clash = configured & set(s.data_access)
    assert not clash, (
        f"{sorted(clash)} already have audit configs, which applying would replace and "
        "destroying would remove: set IKI_GCP_DATA_ACCESS without them"
    )
    if before.binauthz is not None:
        assert before.binauthz.get("defaultAdmissionRule", {}).get(
            "evaluationMode"
        ) == "ALWAYS_ALLOW" and not before.binauthz.get("admissionWhitelistPatterns"), (
            "the project already has a Binary Authorization policy of its own, which the "
            "deployment's would replace"
        )


def kubernetes_version(gcp: Gcp, s: Settings) -> str:
    config = gcp.gcloud_json("container", "get-server-config", "--region", s.region)
    regular = next(c for c in config["channels"] if c["channel"] == "REGULAR")
    major, minor = regular["defaultVersion"].split(".")[:2]
    assert (int(major), int(minor)) >= (1, 36), f"REGULAR's default is {regular['defaultVersion']}"
    return f"{major}.{minor}"


def variables(s: Settings, gcp: Gcp, state_key: str, writers: list[str]) -> dict[str, Any]:
    """The production root's variables: the runbook's, as small as the modules allow."""
    return {
        "name": s.name,
        "project_id": s.project,
        "region": s.region,
        "dr_region": s.dr_region,
        "multi_region": s.multi_region,
        "zones": list(s.zones),
        "kubernetes_version": kubernetes_version(gcp, s),
        # Whoever runs this administers the project already; no one else is added.
        "admin_members": [],
        "dns_endpoint": True,
        "state_kms_key": state_key,
        "public_host": s.host,
        "internal": True,
        "internal_certificate": s.certificate,
        "node_pools": {
            "system": {
                "machine_type": "e2-standard-4",
                "min_size": 2,
                "max_size": 3,
                "disk_size_gib": 50,
            },
            "runs": {
                "machine_type": "e2-standard-2",
                "min_size": 0,
                "max_size": 1,
                "disk_size_gib": 50,
            },
        },
        "database_tier": "db-perf-optimized-N-2",
        "database_replica": False,
        "data_access_services": list(s.data_access),
        "log_retention_days": 30,
        "optional_secrets": ["backend-worker-token"],
        "image_writers": writers,
        # Every run's, besides the modules' iki-deployment=<name>.
        "labels": {"iki-test": "true"},
    }


#: Cloud Build's own Docker daemon has a BuildKit too old for deploy/worker.Dockerfile's
#: secret mount into an environment variable (exec.secretenv), so every image builds in
#: a BuildKit container of its own (buildx's docker-container driver), which pushes it:
#: the bases too, since such a builder can't see images in the daemon. It signs in to
#: Artifact Registry as the build's service account, with the metadata server's token.
CLOUDBUILD = """
steps:
  - id: builder
    name: docker:27.5.1-cli
    entrypoint: sh
    args:
      - -c
      - >-
        wget -qO- --header "Metadata-Flavor: Google"
        http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token
        | sed -E 's/.*"access_token":"([^"]+)".*/\\1/'
        | docker login -u oauth2accesstoken --password-stdin https://${_REGISTRY}
        && docker buildx create --name iki --driver docker-container --bootstrap
  - id: server-base
    name: docker:27.5.1-cli
    entrypoint: docker
    args: [buildx, build, --builder, iki, -f, server/Dockerfile, -t, "${_SERVER_BASE}", --push, .]
    waitFor: [builder]
  - id: server
    name: docker:27.5.1-cli
    entrypoint: docker
    args: [buildx, build, --builder, iki, -f, deploy/plugins.Dockerfile,
           --build-arg, "IKI_SERVER_IMAGE=${_SERVER_BASE}", -t, "${_SERVER}", --push,
           build/gcp-live/server]
    waitFor: [server-base]
  - id: worker-base
    name: docker:27.5.1-cli
    entrypoint: docker
    args: [buildx, build, --builder, iki, -f, worker/Dockerfile, -t, "${_WORKER_BASE}", --push, .]
    waitFor: [builder]
  - id: worker
    name: docker:27.5.1-cli
    entrypoint: docker
    args: [buildx, build, --builder, iki, -f, deploy/worker.Dockerfile,
           --build-arg, "IKI_WORKER_IMAGE=${_WORKER_BASE}", -t, "${_WORKER}", --push,
           build/gcp-live/worker]
    waitFor: [worker-base]
options: {machineType: E2_HIGHCPU_8, diskSizeGb: 100, logging: CLOUD_LOGGING_ONLY}
timeout: 5400s
"""


def digest(gcp: Gcp, ref: str) -> str:
    found = gcp.gcloud(
        "artifacts", "docker", "images", "describe", ref, "--format=value(image_summary.digest)"
    )
    return found.strip()


def write_kubeconfig(gcp: Gcp, s: Settings) -> None:
    """kubectl to the cluster's DNS endpoint (IAM decides), with the token file: what
    get_credentials writes, but reading the token Gcp keeps fresh rather than through
    gke-gcloud-auth-plugin."""
    endpoint = gcp.gcloud(
        "container",
        "clusters",
        "describe",
        s.name,
        "--location",
        s.region,
        "--format=value(controlPlaneEndpointsConfig.dnsEndpointConfig.endpoint)",
    ).strip()
    assert endpoint, "the cluster has no DNS endpoint"
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": s.name, "cluster": {"server": f"https://{endpoint}"}}],
        "users": [{"name": s.name, "user": {"tokenFile": str(gcp.token_file)}}],
        "contexts": [{"name": s.name, "context": {"cluster": s.name, "user": s.name}}],
        "current-context": s.name,
    }
    path = gcp.root / "kubeconfig"
    path.write_text(yaml.safe_dump(config))
    path.chmod(0o600)


def stand_up(d: Deployment) -> None:
    """The runbook, steps 1 to 5, in the work directory's copy of deploy/gcp."""
    s, gcp = d.settings, d.gcp
    work = s.workdir
    shutil.copytree(
        REPO / "deploy" / "gcp",
        work / "deploy" / "gcp",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".terraform", "*.tfstate*", "terraform.tfvars*"),
    )

    # 1. The APIs, and the state's bucket and key.
    boot = (
        "-var",
        f"project_id={s.project}",
        "-var",
        f"name={s.name}",
        "-var",
        f"location={s.multi_region}",
        "-var",
        f'labels={{"{s.name}" = "true"}}',
    )
    gcp.tofu("bootstrap", "init", "-input=false", "-lockfile=readonly", log="bootstrap.log")
    gcp.tofu("bootstrap", "apply", "-input=false", "-auto-approve", *boot, log="bootstrap.log")
    backend = gcp.run("tofu", "-chdir=deploy/gcp/bootstrap", "output", "-raw", "backend_hcl").stdout
    (work / "deploy/gcp/production/backend.hcl").write_text(backend)
    state_key = gcp.run(
        "tofu", "-chdir=deploy/gcp/bootstrap", "output", "-raw", "state_kms_key"
    ).stdout

    # The internal load balancer's certificate, uploaded as the runbook's reader would.
    ca, chain, key = iki_live.make_certificate(work, s.host)
    gcp.gcloud(
        "compute",
        "ssl-certificates",
        "create",
        s.certificate,
        "--region",
        s.region,
        f"--certificate={chain}",
        f"--private-key={key}",
    )

    # 2. The infrastructure. Cloud Build pushes the images, so it's an image writer.
    builder = gcp.gcloud(
        "builds", "get-default-service-account", "--format=value(serviceAccountEmail)"
    )
    writers = [f"serviceAccount:{builder.strip().split('/')[-1]}"]
    (work / "deploy/gcp/production/terraform.tfvars.json").write_text(
        json.dumps(variables(s, gcp, state_key, writers), indent=1)
    )
    gcp.tofu(
        "production", "init", "-input=false", "-lockfile=readonly", "-backend-config=backend.hcl"
    )
    # The repositories first, so the images build while the cluster and database are made.
    gcp.tofu(
        "production",
        "apply",
        "-input=false",
        "-auto-approve",
        "-target=module.registry",
        timeout=1800,
    )
    images = json.loads(
        gcp.run("tofu", "-chdir=deploy/gcp/production", "output", "-json", "images").stdout
    )
    server, worker = f"{images['server']}/iki-server:live", f"{images['worker']}/iki-worker:live"
    registry = images["server"].split("/", 1)[0]
    iki_live.build_plugins(work)
    archive = iki_live.source_archive(work, "build/gcp-live")
    # Tagged, so the cost report finds its builds.
    (work / "cloudbuild.yaml").write_text(f"{CLOUDBUILD}tags: [{s.name}]\n")
    # Started, not followed: each look at it is a new gcloud call, with the current token.
    build = (
        gcp.gcloud(
            "builds",
            "submit",
            str(archive),
            "--async",
            "--format=value(id)",
            "--config",
            str(work / "cloudbuild.yaml"),
            f"--gcs-source-staging-dir=gs://{s.project}_cloudbuild/{s.name}-live",
            f"--substitutions=_SERVER={server},_WORKER={worker},_REGISTRY={registry},"
            f"_SERVER_BASE={images['server']}/iki-server-base:live,"
            f"_WORKER_BASE={images['worker']}/iki-worker-base:live",
            timeout=900,
        )
        .strip()
        .splitlines()[-1]
    )
    # Everything but the cluster first, then the cluster and what needs it: each apply fits
    # in its token's hour, and what Google refuses fails in minutes, before a cluster is
    # made to be torn down. The images build meanwhile, for longer than both.
    gcp.tofu("production", "apply", "-input=false", "-auto-approve", "-exclude=module.cluster")
    # A build that has failed already fails the run now, not after the rest is made.
    early = gcp.gcloud("builds", "describe", build, "--format=value(status)").strip()
    assert early in ("QUEUED", "PENDING", "WORKING", "SUCCESS"), (
        f"build {build}: {early} (gcloud builds log {build})"
    )
    gcp.tofu("production", "apply", "-input=false", "-auto-approve")
    status = until(
        "Cloud Build",
        lambda: (
            st
            if (st := gcp.gcloud("builds", "describe", build, "--format=value(status)").strip())
            not in ("QUEUED", "PENDING", "WORKING")
            else None
        ),
        5400,
        every=30,
    )
    assert status == "SUCCESS", f"build {build}: {status} (gcloud builds log {build})"
    server_digest, worker_digest = digest(gcp, server), digest(gcp, worker)

    # 3. The cluster's controllers.
    write_kubeconfig(gcp, s)
    gcp.helm("repo", "add", "external-secrets", iki_live.EXTERNAL_SECRETS[0])
    gcp.helm(
        "upgrade",
        "--install",
        "external-secrets",
        "external-secrets/external-secrets",
        "--version",
        iki_live.EXTERNAL_SECRETS[1],
        "--namespace",
        "external-secrets",
        "--create-namespace",
        "--wait",
        "--timeout",
        "10m",
    )

    # 4. Secrets and the database's roles.
    gcp.run("sh", "-c", gcp.output("put_secrets"), timeout=300)
    gcp.run("sh", "-c", gcp.output("create_database_roles"), timeout=600)
    gcp.kubectl("apply", "-f", "-", input=gcp.output("external_secrets"))
    # Not iki-run-secrets: with no plugin secrets, External Secrets makes no Secret.
    iki_live.wait_for_secrets(
        gcp,
        (
            "iki-secret-key",
            "iki-encryption-keys",
            "iki-bundle-signing-key",
            "iki-database",
            "iki-database-migrator",
        ),
    )

    # 5. Iki, then its Gateway.
    values = work / "deploy/gcp/production"
    (values / "values-gcp.yaml").write_text(gcp.output("helm_values"))
    live: dict[str, Any] = {
        "environment": "gcp-live",
        "image": {"repository": f"{images['server']}/iki-server", "digest": server_digest},
        # Its plugins come from this checkout, not commits a lockfile pins.
        "plugins": {"allowLocal": True},
        "web": {
            "replicas": 2,
            "autoscaling": {"minReplicas": 2},
            "podDisruptionBudget": {"minAvailable": 1},
        },
        "tasks": {"replicas": 1},
        "egressProxy": {"replicas": 1},
    }
    (values / "values-live.yaml").write_text(yaml.safe_dump(live))
    chart = (
        "upgrade",
        "--install",
        "iki",
        str(REPO / "deploy/helm/iki"),
        "--namespace",
        "iki",
        "-f",
        str(values / "values-production.yaml"),
        "-f",
        str(values / "values-gcp.yaml"),
        "-f",
        str(values / "values-live.yaml"),
    )
    gcp.helm(*chart, "--wait", "--timeout", "20m")
    gcp.kubectl("apply", "-f", "-", input=gcp.output("gateway"))
    gcp.kubectl(
        "-n",
        "iki",
        "wait",
        "--for=condition=Programmed",
        "gateway/iki",
        "--timeout=20m",
        timeout=1300,
    )

    # Backend workers: a registration token a Compute admin makes, kept in Secret Manager
    # and copied in by External Secrets; they trust the test's CA and the public ones.
    token = gcp.server(
        "create_registration_token", "--tier", "backend", "--label", "GCP live test"
    ).strip()
    gcp.gcloud(
        "secrets", "versions", "add", f"{s.name}-backend-worker-token", "--data-file=-", input=token
    )
    gcp.kubectl(
        "-n",
        "iki",
        "annotate",
        "externalsecret",
        "iki-backend-worker-token",
        f"force-sync={int(time.time())}",
        "--overwrite",
    )
    iki_live.wait_for_secrets(gcp, ("iki-backend-worker-token",))
    import certifi  # the public CAs, for Cloud Storage's signed URLs

    bundle = work / "tls" / "workers-ca.crt"
    bundle.write_text(ca.read_text() + Path(certifi.where()).read_text())
    gcp.kubectl(
        "-n", "iki", "create", "secret", "generic", "iki-test-ca", f"--from-file=ca.crt={bundle}"
    )
    live["backendWorkers"] = {
        "enabled": True,
        "image": {"repository": f"{images['worker']}/iki-worker", "digest": worker_digest},
        "registrationToken": {"existingSecret": "iki-backend-worker-token", "key": "token"},
        "caSecret": {"name": "iki-test-ca", "key": "ca.crt"},
        "replicas": 1,
    }
    (values / "values-live.yaml").write_text(yaml.safe_dump(live))
    gcp.helm(*chart, "--timeout", "20m")


@pytest.fixture(scope="module")
def deployment(request: pytest.FixtureRequest) -> Iterator[Deployment]:
    s = Settings.from_env(new=True)
    d = Deployment(
        cloud="gcp",
        name=s.name,
        workdir=s.workdir,
        tools=Gcp(s),
        suite_version=SUITE_VERSION,
        settings=s,
    )
    yield from iki_live.lifecycle(d, request)


@pytest.fixture(scope="module")
def iki(deployment: Deployment) -> Iki:
    """A web pod, through a port-forward, with no one signed in."""
    return iki_live.forward_to_web(deployment.gcp, deployment.forwards, deployment.settings.host)


@pytest.fixture(scope="module")
def admin(deployment: Deployment, iki: Iki) -> Iki:
    """The same, signed in as an Org Admin in a team of their own (iki_live.sign_in_admin)."""
    signed_in, deployment.team = iki_live.sign_in_admin(deployment.gcp, iki, "GCP live test")
    return signed_in


@pytest.fixture(scope="module")
def project(deployment: Deployment, admin: Iki) -> dict[str, Any]:
    return iki_live.make_project(admin, deployment.team)


# --- The checks ------------------------------------------------------------------------------


def test_web_pods_are_ready_with_their_database(iki: Iki) -> None:
    iki_live.check_ready(iki)


def test_the_database_is_reached_privately_and_over_verified_tls(deployment: Deployment) -> None:
    gcp, s = deployment.gcp, deployment.settings
    found = iki_live.database_connection(gcp)
    # The range the network module peers with Google's managed services: the last /20 of
    # vpc_cidr's default, 10.40.0.0/16.
    managed = ipaddress.IPv4Network("10.40.240.0/20")
    assert ipaddress.IPv4Address(found["address"]) in managed, found
    instance = gcp.gcloud_json("sql", "instances", "describe", s.name)
    assert [a["type"] for a in instance["ipAddresses"]] == ["PRIVATE"], instance["ipAddresses"]
    assert instance["settings"]["ipConfiguration"]["sslMode"] == "ENCRYPTED_ONLY"


def test_the_setup_link_opens_the_wizard(deployment: Deployment, iki: Iki) -> None:
    iki_live.check_setup_link(deployment.gcp, iki, deployment.settings.host)


def test_an_org_admin_creates_a_project_and_an_entity(admin: Iki, project: dict[str, Any]) -> None:
    iki_live.check_project_and_entity(admin, project)


def test_a_file_goes_up_and_comes_down_through_urls_iam_signed(
    admin: Iki, project: dict[str, Any]
) -> None:
    def upload(url: str) -> None:
        # Signed by IAM's signBlob, as the server's service account: no key anywhere.
        assert urlsplit(url).hostname == "storage.googleapis.com", url
        assert "X-Goog-Algorithm=GOOG4-RSA-SHA256" in url, url

    def download(url: str) -> None:
        assert urlsplit(url).hostname == "storage.googleapis.com", url

    iki_live.check_file_round_trip(admin, project, upload, download)


def test_a_backend_worker_computes_a_proteins_properties(
    admin: Iki, project: dict[str, Any]
) -> None:
    iki_live.check_backend_run(admin, project)


def test_setup_storage_finds_the_bucket_as_iki_needs_it(deployment: Deployment) -> None:
    iki_live.check_setup_storage(deployment.gcp)


def test_tearing_down_leaves_only_what_google_cloud_keeps(deployment: Deployment) -> None:
    """Last: everything goes, and nothing else in the project changed."""
    problems = deployment.tear_down()
    deployment.torn_down_checked = True
    assert not problems, "\n".join(problems)
