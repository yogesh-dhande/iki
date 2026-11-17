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
log bucket and custom roles pending deletion) or an API it turned on.

It costs: a regional GKE cluster's fee and three or four small nodes, a highly
available Cloud SQL Enterprise Plus instance (2 vCPUs), Cloud NAT, an internal load
balancer and two Cloud Build builds: a few US dollars for its hour or two. It never runs
on CI (conftest.py).

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
import hashlib
import http.cookiejar
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import gcp_cost
import pytest
import requests
import yaml

pytestmark = pytest.mark.gcp_live

REPO = Path(__file__).resolve().parents[3]
#: External Secrets' chart, as the runbook installs it: pinned to a release qualified here.
EXTERNAL_SECRETS = ("https://charts.external-secrets.io", "2.15.0")
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
#: The protein the backend run computes the properties of: human haemoglobin's beta chain.
HBB = (
    "MVHLTPEEKSAVTALWGKVNVDEVGGEALGRLLVVYPWTQRFFESFGDLSTPDAVMGNPKVKAHGKKVLGAFSDGLAHLD"
    "NLKGTFATLSELHCDKLHVDPENFRLLGNVLVCVLAHHFGKEFTPPVQAAYQKVVAGVANALAHKYH"
)


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
        its own, iki-test-<month><day><hour><minute> in UTC, and is remembered as the last
        run, which a later look (the cost report) takes."""
        env = os.environ
        last = REPO / "build" / "gcp-live" / "last-run"
        name = env.get("IKI_GCP_NAME", "")
        if not name and new:
            name = "iki-test-" + datetime.datetime.now(datetime.UTC).strftime("%m%d%H%M")
            last.parent.mkdir(parents=True, exist_ok=True)
            last.write_text(name)
        elif not name:
            name = last.read_text().strip()
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


class TokenError(RuntimeError):
    """No access token could be had: stop making anything."""


class Gcp:
    """Runs the command-line tools in the work directory's copy of deploy/gcp, all as one
    identity: an OAuth access token from IKI_GCP_TOKEN_COMMAND, minted again every 20
    minutes, kept in a file only this user can read. gcloud reads it from that file
    (CLOUDSDK_AUTH_ACCESS_TOKEN_FILE) with a gcloud configuration of its own, so the
    caller's accounts and settings are never used or changed; kubectl from the same file
    (the kubeconfig's tokenFile); OpenTofu from GOOGLE_OAUTH_ACCESS_TOKEN, set fresh for
    each run. Helm has its own repositories here too. The token is never in a command
    line, a log or an error message."""

    #: How often the token is minted again: an hour's token then has 40 minutes left at
    #: worst, longer than any one apply or destroy here.
    REFRESH = 20 * 60

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.workdir
        self.secret_dir = Path(tempfile.mkdtemp(prefix="iki-gcp-live-"))
        self.secret_dir.chmod(0o700)
        self.token_file = self.secret_dir / "token"
        self.minted = 0.0
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        env = {k: v for k, v in os.environ.items() if k not in _CREDENTIAL_VARIABLES}
        env.update(
            CLOUDSDK_CONFIG=str(self.secret_dir / "gcloud"),
            CLOUDSDK_AUTH_ACCESS_TOKEN_FILE=str(self.token_file),
            CLOUDSDK_CORE_PROJECT=settings.project,
            CLOUDSDK_CORE_DISABLE_PROMPTS="1",
            KUBECONFIG=str(self.root / "kubeconfig"),
            HELM_CONFIG_HOME=str(self.root / "helm" / "config"),
            HELM_CACHE_HOME=str(self.root / "helm" / "cache"),
            HELM_DATA_HOME=str(self.root / "helm" / "data"),
            TF_IN_AUTOMATION="1",
            TF_INPUT="0",
            IKI_TEARDOWN_YES="1",
        )
        self.env = env
        self.mint()
        self.refresher = threading.Thread(target=self._refresh, daemon=True)
        self.refresher.start()

    def mint(self) -> None:
        """A new token in the token file, trying for five minutes before giving up."""
        with self.lock:
            command = self.settings.token_command
            for attempt in range(11):
                result = subprocess.run(  # noqa: S603 - the caller's own command
                    shlex.split(command), capture_output=True, text=True, check=False, timeout=120
                )
                token = result.stdout.strip().splitlines()[-1:] if result.returncode == 0 else []
                if token and re.fullmatch(r"[A-Za-z0-9._\-~+/]+=*", token[0]):
                    # Written beside it and renamed over it: a reader (gcloud, tofu)
                    # never finds it empty or half written.
                    fresh = self.token_file.with_name("token.new")
                    descriptor = os.open(fresh, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(descriptor, "w") as out:
                        out.write(token[0])
                    fresh.replace(self.token_file)
                    self.minted = time.monotonic()
                    return
                if attempt < 10:
                    time.sleep(30)
            # Neither the output nor the error: either might hold a token.
            raise TokenError(
                f"IKI_GCP_TOKEN_COMMAND failed (exit {result.returncode}) for five minutes"
            )

    def _refresh(self) -> None:
        while not self.stopped.wait(self.REFRESH):
            try:
                self.mint()
            except TokenError:
                return

    def access_token(self) -> str:
        """The current token, for a REST call of the test's own (gcp_cost.py)."""
        self._env()
        return self.token_file.read_text()

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

    def stop(self) -> None:
        self.stopped.set()
        shutil.rmtree(self.secret_dir, ignore_errors=True)

    def _env(self) -> dict[str, str]:
        if time.monotonic() - self.minted > 50 * 60:
            raise TokenError(
                "the access token is older than 50 minutes and couldn't be minted again"
            )
        return {**self.env, "GOOGLE_OAUTH_ACCESS_TOKEN": self.token_file.read_text()}

    def run(
        self,
        *argv: str,
        input: str | None = None,
        check: bool = True,
        timeout: float = 600,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(  # noqa: S603 - argv built here, never a shell
            list(argv),
            input=input,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            cwd=cwd or self.root,
            env=self._env(),
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"{' '.join(argv[:4])} ... exited {result.returncode}:\n"
                f"{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
            )
        return result

    def stream(self, *argv: str, log: str, timeout: float = 3600, cwd: Path | None = None) -> None:
        """Runs a long command with its output in a log file of the work directory."""
        path = self.root / "logs" / log
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as out:
            out.write(f"\n$ {' '.join(argv)}\n")
            out.flush()
            result = subprocess.run(  # noqa: S603 - argv built here
                list(argv),
                stdout=out,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=timeout,
                cwd=cwd or self.root,
                env=self._env(),
            )
        if result.returncode != 0:
            tail = path.read_text()[-6000:]
            raise AssertionError(f"{' '.join(argv[:4])} ... exited {result.returncode}:\n{tail}")

    def popen(self, *argv: str, log: str) -> subprocess.Popen[bytes]:
        path = self.root / "logs" / log
        path.parent.mkdir(parents=True, exist_ok=True)
        out = path.open("ab")
        return subprocess.Popen(  # noqa: S603 - argv built here
            list(argv), stdout=out, stderr=subprocess.STDOUT, cwd=self.root, env=self._env()
        )

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

    def kubectl(self, *args: str, **kwargs: Any) -> str:
        return self.run("kubectl", *args, **kwargs).stdout

    def helm(self, *args: str, timeout: float = 1800) -> None:
        self.stream("helm", *args, log="helm.log", timeout=timeout)

    def shell(self, code: str) -> str:
        """Python in a web pod, as the server: iki-server shell."""
        return self.kubectl(
            "-n",
            "iki",
            "exec",
            "deploy/iki-web",
            "-c",
            "web",
            "--",
            "iki-server",
            "shell",
            "-c",
            code,
        )


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
        lines: list[str] = []
        for section, found in self.listings.items():
            lines += [f"=== {section}", *sorted(found), ""]
        for key, versions in sorted(self.key_versions.items()):
            lines += [f"=== kms key versions of {key}", *sorted(versions), ""]
        lines += ["=== project IAM (role member)", *sorted(" ".join(b) for b in self.bindings), ""]
        lines += ["=== audit configs", json.dumps(self.audit_configs, indent=1, sort_keys=True)]
        lines += ["", "=== binary authorization policy", json.dumps(self.binauthz, sort_keys=True)]
        path.write_text("\n".join(lines) + "\n")


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


def key_rings(gcp: Gcp, s: Settings) -> list[str]:
    """The deployment's key rings, where it makes them."""
    found = []
    for location in sorted({s.region, s.dr_region, s.multi_region}):
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


# --- A certificate for the internal load balancer -------------------------------------------


def make_certificate(root: Path, host: str) -> tuple[Path, Path, Path]:
    """A CA of the test's own, and a certificate it signs for host: openssl, as the
    runbook's readers would. Returns the CA, the certificate with its chain, and the key."""
    tls = root / "tls"
    tls.mkdir(parents=True, exist_ok=True)
    ca, ca_key, key, csr, cert = (
        tls / n for n in ("ca.crt", "ca.key", "tls.key", "tls.csr", "tls.crt")
    )
    if cert.exists():
        return ca, tls / "chain.crt", key

    def openssl(*args: str) -> None:
        subprocess.run(["openssl", *args], check=True, capture_output=True, timeout=60)  # noqa: S603, S607

    openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "2",
        "-subj",
        f"/CN={host} test CA",
        "-keyout",
        str(ca_key),
        "-out",
        str(ca),
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
    )
    openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-subj",
        f"/CN={host}",
        "-keyout",
        str(key),
        "-out",
        str(csr),
    )
    extensions = tls / "leaf.ext"
    extensions.write_text(
        f"subjectAltName=DNS:{host}\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n"
    )
    openssl(
        "x509",
        "-req",
        "-in",
        str(csr),
        "-CA",
        str(ca),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-days",
        "2",
        "-extfile",
        str(extensions),
        "-out",
        str(cert),
    )
    (tls / "chain.crt").write_text(cert.read_text() + ca.read_text())
    return ca, tls / "chain.crt", key


# --- Iki, through a port-forward ------------------------------------------------------------


class Iki:
    """HTTP to a web pod through kubectl port-forward, as a browser at publicUrl would
    send it: the Host it names, HTTPS as the load balancer forwards it. Iki's cookies are
    __Host- (Secure), so they're kept here and sent by hand over the forward's plain HTTP."""

    def __init__(self, port: int, host: str) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.public = f"https://{host}"
        self.host = host
        self.cookies: dict[str, str] = {}
        self.http = requests.Session()
        self.http.trust_env = False
        # The cookies above, not the session's: it wouldn't send Secure ones over HTTP.
        self.http.cookies.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        headers = {
            "Host": self.host,
            "X-Forwarded-Proto": "https",
            "Origin": self.public,
            "Referer": f"{self.public}{path}",
            **kwargs.pop("headers", {}),
        }
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        response = self.http.request(
            method,
            f"{self.base}{path}",
            headers=headers,
            allow_redirects=False,
            timeout=60,
            **kwargs,
        )
        for cookie in response.cookies:
            if cookie.value:
                self.cookies[cookie.name] = cookie.value
        return response

    def page(self, path: str) -> requests.Response:
        """GET, following redirects within Iki."""
        response = self.request("GET", path)
        for _ in range(5):
            if response.status_code not in (301, 302, 303):
                break
            response = self.request("GET", urlsplit(response.headers["Location"]).path or "/")
        return response

    def csrf(self, page: requests.Response) -> str:
        match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text) or re.search(
            r'<meta name="csrf-token" content="([^"]+)"', page.text
        )
        assert match, f"{page.url} carries no CSRF token"
        return match.group(1)

    def api(self, method: str, path: str, body: Any = None) -> requests.Response:
        headers = {}
        if method != "GET":
            headers["X-CSRFToken"] = self.csrf(self.page("/projects/"))
        return self.request(method, f"/api/v1{path}", json=body, headers=headers)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def until(what: str, check: Callable[[], Any], within: float, every: float = 10) -> Any:
    """check's first truthy answer, asked every few seconds."""
    deadline = time.monotonic() + within
    while True:
        found = check()
        if found:
            return found
        if time.monotonic() > deadline:
            raise AssertionError(f"{what}: not within {within:.0f} seconds (last: {found!r})")
        time.sleep(every)


# --- The deployment --------------------------------------------------------------------------


@dataclass
class Deployment:
    settings: Settings
    gcp: Gcp
    before: Inventory | None = None
    after: Inventory | None = None
    torn_down: bool = False
    torn_down_checked: bool = False
    iki: Iki | None = None
    team: str = ""
    forwards: list[subprocess.Popen[bytes]] = field(default_factory=list)
    teardown_errors: list[str] = field(default_factory=list)

    def tear_down(self) -> list[str]:
        """Deletes everything, once, and lists what's left that shouldn't be."""
        if self.torn_down:
            return (
                leftovers(self.before, self.after, self.settings)
                if self.before and self.after
                else []
            )
        self.torn_down = True
        s, gcp = self.settings, self.gcp
        for forward in self.forwards:
            forward.terminate()
        steps: list[tuple[str, Callable[[], object]]] = [
            ("a fresh token", gcp.mint),
            ("the facts the cost report needs", lambda: record_facts(gcp, s)),
            (
                "teardown.sh",
                lambda: run_teardown(gcp, s),
            ),
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
        for what, step in steps:
            try:
                step()
            except Exception as error:
                self.teardown_errors.append(f"{what}: {error}")
        self.after = inventory(gcp, key_rings(gcp, s))
        self.after.write(s.workdir / "gcp-after.txt")
        problems = leftovers(self.before, self.after, s) if self.before else []
        return [*self.teardown_errors, *problems]


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


def cost_report(gcp: Gcp, s: Settings, start: datetime.datetime) -> str:
    """What the run cost, at list prices (gcp_cost.py), in the work directory."""
    end = datetime.datetime.now(datetime.UTC)
    window_file = s.workdir / "gcp-window.json"
    window_file.write_text(json.dumps({"start": start.isoformat(), "end": end.isoformat()}))
    tfvars = s.workdir / "deploy/gcp/production/terraform.tfvars.json"
    variables = json.loads(tfvars.read_text()) if tfvars.exists() else {}
    facts_file = s.workdir / "gcp-facts.json"
    facts = json.loads(facts_file.read_text()) if facts_file.exists() else {}
    window = gcp_cost.Window(s.project, s.name, s.region, start, end)
    return gcp_cost.write(
        gcp_cost.report(gcp, window, variables, facts, s.workdir / "skus"), s.workdir
    )


def check_preconditions(gcp: Gcp, s: Settings, before: Inventory) -> None:
    """Refuses to start where applying would change what isn't the test's."""
    for tool in ("gcloud", "tofu", "kubectl", "helm", "jq", "curl", "openssl", "uv"):
        assert shutil.which(tool), f"{tool} is needed"
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


def _tool(name: str) -> str:
    found = shutil.which(name)
    assert found, f"{name} is needed"
    return found


def source_archive(root: Path) -> Path:
    """The repository at HEAD, and the plugins built for this deployment, for Cloud Build."""
    archive = root / "source.tar"
    subprocess.run(  # noqa: S603 - argv built here
        [_tool("git"), "-C", str(REPO), "archive", "--format=tar", "-o", str(archive), "HEAD"],
        check=True,
        timeout=300,
    )
    with tarfile.open(archive, "a") as tar:
        tar.add(root / "plugins-build", arcname="build/gcp-live")
    gz = root / "source.tar.gz"
    subprocess.run(["gzip", "-f", str(archive)], check=True, timeout=300)  # noqa: S603, S607
    return gz


def build_plugins(root: Path) -> None:
    """The server's and the worker's plugins: iki/dna and iki/protein, their tools bound
    to the backend workers (as deploy/dev builds them)."""
    out = root / "plugins-build"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    (out / "plugins.lock.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "iki/v1",
                "kind": "PluginLock",
                "plugins": [
                    {"name": f"iki/{n}", "local": str(REPO / "plugins" / n), "state": "active"}
                    for n in ("dna", "protein")
                ],
            }
        )
    )
    documents = [
        {
            "apiVersion": "iki/v1",
            "kind": "ComputeTemplate",
            "name": "backend-small",
            "provisioner": "backend_pool",
            "capacity": {"cpu": 0.25, "memory": "2Gi", "scratch_disk": "2Gi"},
        },
        *(
            {
                "apiVersion": "iki/v1",
                "kind": "EnvironmentBinding",
                "environment": e,
                "templates": [{"template": "backend-small"}],
            }
            for e in ("iki/dna.sequence-tools", "iki/protein.protein-tools")
        ),
    ]
    (out / "compute.yaml").write_text(yaml.safe_dump_all(documents))
    subprocess.run(  # noqa: S603 - argv built here
        [
            _tool("uv"),
            "run",
            "--frozen",
            "iki",
            "deploy",
            "build",
            str(out),
            "--output",
            str(out / "server" / "plugins"),
            "--workers",
            str(out / "worker"),
            "--allow-local",
        ],
        check=True,
        cwd=REPO,
        timeout=1200,
        capture_output=True,
    )


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


def diagnose(gcp: Gcp) -> None:
    """What the cluster says, in logs/diagnose.log, when standing up fails: once it's torn
    down, nothing tells why a pod never started."""
    if not (gcp.root / "kubeconfig").exists():
        return
    for args in (
        ("get", "nodes", "-o", "wide"),
        ("get", "pods", "-A", "-o", "wide"),
        ("get", "events", "-A", "--sort-by=.lastTimestamp"),
        ("describe", "pods", "-n", "external-secrets"),
        ("describe", "pods", "-n", "iki"),
    ):
        try:
            gcp.stream("kubectl", *args, log="diagnose.log", timeout=120)
        except Exception as error:  # whatever it says, the teardown goes on
            sys.stdout.write(f"kubectl {' '.join(args)}: {error!r}\n")
    # Whether a pod resolves public names and reaches them (an image allowed in, which
    # Google's own registry serves through the VPC's private zones).
    probe = (
        "getent ahosts ghcr.io; getent ahosts github.com;"
        " timeout 10 bash -c '</dev/tcp/ghcr.io/443' && echo ghcr.io:443 reached"
    )
    probing: tuple[tuple[str, ...], ...] = (
        (
            "-n",
            "default",
            "run",
            "iki-probe",
            "--image=mirror.gcr.io/library/postgres:17",
            "--restart=Never",
            "--command",
            "--",
            "bash",
            "-c",
            probe,
        ),
        (
            "-n",
            "default",
            "wait",
            "--for=jsonpath={.status.phase}=Succeeded",
            "pod/iki-probe",
            "--timeout=120s",
        ),
        ("-n", "default", "logs", "iki-probe"),
        ("-n", "default", "describe", "pod", "iki-probe"),
    )
    for step in probing:
        try:
            gcp.stream("kubectl", *step, log="diagnose.log", timeout=180)
        except Exception as error:  # whatever it says, the teardown goes on
            sys.stdout.write(f"kubectl {' '.join(step[:4])}: {error!r}\n")


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
    ca, chain, key = make_certificate(work, s.host)
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
    build_plugins(work)
    archive = source_archive(work)
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
    gcp.helm("repo", "add", "external-secrets", EXTERNAL_SECRETS[0])
    gcp.helm(
        "upgrade",
        "--install",
        "external-secrets",
        "external-secrets/external-secrets",
        "--version",
        EXTERNAL_SECRETS[1],
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
    for secret in (
        "iki-secret-key",
        "iki-encryption-keys",
        "iki-bundle-signing-key",
        "iki-database",
        "iki-database-migrator",
        "iki-run-secrets",
    ):
        until(
            f"Secret {secret}",
            lambda n=secret: (
                gcp.run(  # type: ignore[misc]
                    "kubectl", "-n", "iki", "get", "secret", n, check=False
                ).returncode
                == 0
            ),
            300,
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
    token = gcp.kubectl(
        "-n",
        "iki",
        "exec",
        "deploy/iki-web",
        "-c",
        "web",
        "--",
        "iki-server",
        "create_registration_token",
        "--tier",
        "backend",
        "--label",
        "GCP live test",
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
    until(
        "Secret iki-backend-worker-token",
        lambda: (
            gcp.run(
                "kubectl", "-n", "iki", "get", "secret", "iki-backend-worker-token", check=False
            ).returncode
            == 0
        ),
        300,
    )
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
def deployment() -> Iterator[Deployment]:
    s = Settings.from_env(new=True)
    s.workdir.mkdir(parents=True, exist_ok=True)
    gcp = Gcp(s)
    d = Deployment(s, gcp)
    try:
        d.before = inventory(gcp, key_rings(gcp, s))
        d.before.write(s.workdir / "gcp-before.txt")
        check_preconditions(gcp, s, d.before)
    except BaseException:
        gcp.stop()
        raise
    started = datetime.datetime.now(datetime.UTC)
    (s.workdir / "gcp-window.json").write_text(json.dumps({"start": started.isoformat()}))
    try:
        try:
            stand_up(d)
        except BaseException:
            diagnose(gcp)
            raise
        yield d
    finally:
        problems = d.tear_down()
        up = datetime.datetime.now(datetime.UTC) - started
        (s.workdir / "gcp-summary.txt").write_text(f"resources up for {up}\n" + "\n".join(problems))
        try:
            sys.stdout.write("\n" + cost_report(gcp, s, started))
        except Exception as error:
            sys.stdout.write(f"\nThe cost report failed: {error!r}\n")
        gcp.stop()
        if problems and not d.torn_down_checked:
            pytest.fail("tearing down left:\n" + "\n".join(problems))


@pytest.fixture(scope="module")
def iki(deployment: Deployment) -> Iki:
    """A web pod, through a port-forward, with no one signed in."""
    gcp = deployment.gcp
    pods = json.loads(
        gcp.kubectl(
            "-n", "iki", "get", "pods", "-l", "app.kubernetes.io/component=web", "-o", "json"
        )
    )
    ready = [
        p["metadata"]["name"]
        for p in pods["items"]
        if p["metadata"].get("deletionTimestamp") is None
        and all(c["ready"] for c in p["status"].get("containerStatuses", [{"ready": False}]))
    ]
    assert ready, "no web pod is ready"
    port = free_port()
    deployment.forwards.append(
        gcp.popen(
            "kubectl",
            "-n",
            "iki",
            "port-forward",
            f"pod/{ready[0]}",
            f"{port}:8000",
            log="port-forward.log",
        )
    )
    until(
        "the port-forward",
        lambda: socket.socket().connect_ex(("127.0.0.1", port)) == 0,
        30,
        every=0.5,
    )
    return Iki(port, deployment.settings.host)


@pytest.fixture(scope="module")
def admin(deployment: Deployment, iki: Iki) -> Iki:
    """The same, signed in as an Org Admin in a team of their own. The deployment has no
    identity provider, so the session is made in a web pod, as Django's test client makes
    one (force_login): real sign-in isn't exercised here."""
    email = f"gcp-live-{secrets.token_hex(4)}@example.com"
    made = deployment.gcp.shell(f"""
import json
from django.conf import settings
from django.test import Client
from django.utils import timezone
from iki_server.accounts.models import User
from iki_server.org.models import OrgRole, OrgRoleAssignment, Team, TeamMembership
from iki_server.setup.models import Installation
Installation.objects.update_or_create(
    pk=1,
    defaults={{"organization_name": "Iki live test", "setup_completed_at": timezone.now()}},
)
user = User.objects.create_user({email!r}, "GCP live test")
OrgRoleAssignment.objects.create(user=user, role=OrgRole.ORG_ADMIN)
team, _ = Team.objects.get_or_create(name="GCP live test")
TeamMembership.objects.get_or_create(team=team, user=user)
client = Client()
client.force_login(user)
name = settings.SESSION_COOKIE_NAME
print(json.dumps({{"name": name, "value": client.cookies[name].value, "team": str(team.id)}}))
""")
    session = json.loads(made.strip().splitlines()[-1])
    signed_in = Iki(int(iki.base.rsplit(":", 1)[1]), deployment.settings.host)
    signed_in.cookies[session["name"]] = session["value"]
    deployment.team = session["team"]
    return signed_in


@pytest.fixture(scope="module")
def project(deployment: Deployment, admin: Iki) -> dict[str, Any]:
    created = admin.api("POST", "/projects", {"team": deployment.team, "name": "Live check"})
    assert created.status_code == 201, created.text[:500]
    found: dict[str, Any] = created.json()
    assert found["my_role"] == "admin"
    return found


# --- The checks ------------------------------------------------------------------------------


def test_web_pods_are_ready_with_their_database(iki: Iki) -> None:
    alive = iki.request("GET", "/healthz/live")
    assert alive.status_code == 200, alive.text
    ready = iki.request("GET", "/healthz/ready")
    assert ready.json() == {
        "status": "ok",
        "checks": {"database": "ok", "migrations": "ok", "database_role": "ok"},
    }


def test_the_database_is_reached_privately_and_over_verified_tls(deployment: Deployment) -> None:
    gcp, s = deployment.gcp, deployment.settings
    found = json.loads(
        gcp.shell("""
import json
from django.db import connection
with connection.cursor() as cursor:
    cursor.execute("SELECT ssl, version FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
    ssl, version = cursor.fetchone()
    cursor.execute("SELECT host(inet_server_addr())")
    (address,) = cursor.fetchone()
print(json.dumps({"ssl": ssl, "version": version, "address": address,
                  "sslmode": connection.get_connection_params().get("sslmode")}))
""")
        .strip()
        .splitlines()[-1]
    )
    assert found["ssl"] and found["version"].startswith("TLSv1."), found
    assert found["sslmode"] == "verify-full", found
    # The range the network module peers with Google's managed services: the last /20 of
    # vpc_cidr's default, 10.40.0.0/16.
    managed = ipaddress.IPv4Network("10.40.240.0/20")
    assert ipaddress.IPv4Address(found["address"]) in managed, found
    instance = gcp.gcloud_json("sql", "instances", "describe", s.name)
    assert [a["type"] for a in instance["ipAddresses"]] == ["PRIVATE"], instance["ipAddresses"]
    assert instance["settings"]["ipConfiguration"]["sslMode"] == "ENCRYPTED_ONLY"


def test_the_setup_link_opens_the_wizard(deployment: Deployment, iki: Iki) -> None:
    out = deployment.gcp.kubectl(
        "-n", "iki", "exec", "deploy/iki-web", "-c", "web", "--", "iki-server", "setup_link"
    )
    match = re.search(rf"https://{re.escape(deployment.settings.host)}/setup/#(\S+)", out)
    assert match, out
    start = iki.page("/setup/")
    assert start.status_code == 200, start.text[:500]
    sent = iki.request(
        "POST", "/setup/", data={"csrfmiddlewaretoken": iki.csrf(start), "token": match.group(1)}
    )
    assert sent.status_code == 302 and sent.headers["Location"].endswith("/setup/organization/"), (
        sent.text[:500]
    )
    organization = iki.page("/setup/organization/")
    assert organization.status_code == 200 and "organization_name" in organization.text


def test_an_org_admin_creates_a_project_and_an_entity(admin: Iki, project: dict[str, Any]) -> None:
    me = admin.api("GET", "/me")
    assert me.status_code == 200 and "org_admin" in me.json()["org_roles"], me.text[:300]
    made = admin.api(
        "POST",
        "/entities",
        {"type": "iki/protein.Protein", "project": project["id"], "values": {"sequence": HBB}},
    )
    assert made.status_code in (200, 201), made.text[:500]
    got = admin.api("GET", f"/entities/{made.json()['id']}")
    assert got.json()["values"]["sequence"] == HBB


def test_a_file_goes_up_and_comes_down_through_urls_iam_signed(
    admin: Iki, project: dict[str, Any]
) -> None:
    body = os.urandom(6 * 1024 * 1024) + b"iki live"
    sha = hashlib.sha256(body).hexdigest()
    started = admin.api(
        "POST",
        "/uploads",
        {
            "project": project["id"],
            "name": "live.bin",
            "media_type": "application/octet-stream",
            "size": len(body),
            "sha256": sha,
        },
    )
    assert started.status_code == 201, started.text[:500]
    upload = started.json()
    offset = 0
    for part in upload["parts"]:
        # Signed by IAM's signBlob, as the server's service account: no key anywhere.
        assert urlsplit(part["url"]).hostname == "storage.googleapis.com", part["url"]
        assert "X-Goog-Algorithm=GOOG4-RSA-SHA256" in part["url"], part["url"]
        sent = requests.put(part["url"], data=body[offset : offset + part["size"]], timeout=120)
        assert sent.status_code == 200, sent.text[:500]
        offset += part["size"]
    done = admin.api("POST", f"/uploads/{upload['id']}/complete")
    assert done.status_code in (200, 202), done.text[:500]
    ready = until(
        "the upload's check",
        lambda: (
            u
            if (u := admin.api("GET", f"/uploads/{upload['id']}").json())["state"]
            in ("ready", "failed")
            else None
        ),
        300,
        every=3,
    )
    assert ready["state"] == "ready", ready
    link = admin.api("GET", f"/files/{ready['file']['id']}/content-url").json()["url"]
    assert urlsplit(link).hostname == "storage.googleapis.com"
    downloaded = requests.get(link, timeout=120)
    assert downloaded.status_code == 200 and hashlib.sha256(downloaded.content).hexdigest() == sha


def test_a_backend_worker_computes_a_proteins_properties(
    admin: Iki, project: dict[str, Any]
) -> None:
    made = admin.api(
        "POST",
        "/entities",
        {"type": "iki/protein.Protein", "project": project["id"], "values": {"sequence": HBB}},
    )
    protein = made.json()
    launched = admin.api(
        "POST",
        "/actions/iki/protein.compute_properties/runs",
        {
            "project": project["id"],
            "inputs": {"protein": protein["id"]},
            "params": {},
        },
    )
    assert launched.status_code == 201, launched.text[:500]
    run_id = launched.json()["id"]
    # The runs pool starts at no nodes: the autoscaler adds one for the worker first.
    run = until(
        "the run",
        lambda: (
            r
            if (r := admin.api("GET", f"/runs/{run_id}").json())["status"]
            in ("succeeded", "failed", "cancelled")
            else None
        ),
        1500,
        every=15,
    )
    assert run["status"] == "succeeded", run
    values = admin.api("GET", f"/entities/{protein['id']}").json()["values"]
    assert values["length"] == len(HBB), values
    assert 0 < values["isoelectric_point"] < 14, values


def test_setup_storage_finds_the_bucket_as_iki_needs_it(deployment: Deployment) -> None:
    checked = deployment.gcp.run(
        "kubectl",
        "-n",
        "iki",
        "exec",
        "deploy/iki-web",
        "-c",
        "web",
        "--",
        "iki-server",
        "setup_storage",
        "--check",
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_tearing_down_leaves_only_what_google_cloud_keeps(deployment: Deployment) -> None:
    """Last: everything goes, and nothing else in the project changed."""
    problems = deployment.tear_down()
    deployment.torn_down_checked = True
    assert not problems, "\n".join(problems)
