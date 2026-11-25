"""What the live tests share: each deploys Iki to a real cloud, checks it and tears it down.

deploy/gcp/tests/test_gcp_live.py and deploy/aws/tests/test_aws_live.py each stand up their
cloud's production root as its runbook does, run the same checks through a port-forward to
a web pod, and always tear down, then list the cloud again and fail if anything of the
deployment's is left. This module is the part that isn't any one cloud's:

- whether a live test runs at all (``skip_reason``): only when asked for, never on CI;
- a run's name, ``iki-test-<MMDDHHMM>``, and the last run's, for a later look;
- ``Token``: the one credential the tools act with, minted by a command, kept in a file
  only this user can read, written whole and renamed into place, and minted again before
  it expires;
- ``Tools``: kubectl, Helm and the cloud's own tools, run in the work directory with that
  credential and nothing of the caller's;
- inventories, written out and compared;
- a certificate for an internal load balancer, the plugins and the source the images are
  built from, and diagnostics when standing up fails;
- ``Iki``: HTTP to a web pod through ``kubectl port-forward``, and the checks themselves;
- ``Live`` and ``lifecycle``: the module fixture's shape. It tears down, always, even when
  standing up or a check failed, prices the run, and records a passing one in
  deploy/live-runs.json (live_runs.py).
"""

import datetime
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import live_runs
import pytest
import requests
import yaml

REPO = Path(__file__).resolve().parents[2]
#: External Secrets' chart, as both runbooks install it: pinned to a release qualified here.
EXTERNAL_SECRETS = ("https://charts.external-secrets.io", "2.15.0")
#: Iki's plugins the deployment installs (plugins/<name>), and the action of theirs the
#: backend run launches: live_runs.RELEVANT covers them, since the run is what tests them.
PLUGINS = ("dna", "protein")
ACTION = "iki/protein.compute_properties"
#: The protein the backend run computes the properties of: human haemoglobin's beta chain.
HBB = (
    "MVHLTPEEKSAVTALWGKVNVDEVGGEALGRLLVVYPWTQRFFESFGDLSTPDAVMGNPKVKAHGKKVLGAFSDGLAHLD"
    "NLKGTFATLSELHCDKLHVDPENFRLLGNVLVCVLAHHFGKEFTPPVQAAYQKVVAGVANALAHKYH"
)


# --- Whether it runs -----------------------------------------------------------------------


def skip_reason(
    config: pytest.Config, *, cloud: str, flag: str, required: Mapping[str, str]
) -> str | None:
    """Why a live test doesn't run here, or None if it does: it needs its flag and each of
    the required variables (name: what it is), and never runs on CI (every CI service sets
    CI), since it bills a real account."""
    if os.environ.get("CI"):
        return f"the live {cloud} test never runs on CI: it bills a real account"
    if not config.getoption(flag):
        return f"the live {cloud} test: run it with {flag}"
    for variable, what in required.items():
        if not os.environ.get(variable):
            return f"the live {cloud} test needs {variable}, {what}"
    return None


def skip_unless_asked(
    config: pytest.Config, items: list[pytest.Item], marker: str, reason: str | None
) -> None:
    if reason is None:
        return
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if marker in item.keywords:
            item.add_marker(skip)


# --- A run's name --------------------------------------------------------------------------


def run_name(cloud: str, given: str, *, new: bool) -> str:
    """The run's name: given, or for a new run one of its own, iki-test-<month><day><hour>
    <minute> in UTC, remembered as the last run (build/<cloud>-live/last-run), which a
    later look (the cost report) takes when it's given none."""
    last = REPO / "build" / f"{cloud}-live" / "last-run"
    if given:
        return given
    if new:
        name = "iki-test-" + datetime.datetime.now(datetime.UTC).strftime("%m%d%H%M")
        last.parent.mkdir(parents=True, exist_ok=True)
        last.write_text(name)
        return name
    return last.read_text().strip()


# --- The one credential --------------------------------------------------------------------


class TokenError(RuntimeError):
    """No token could be had: stop making anything."""


#: What a token may look like: an OAuth access token, or a JWT (an identity token).
TOKEN = re.compile(r"[A-Za-z0-9._\-~+/]+=*")


class Token:
    """A credential the command prints (its last line), in a file only this user can read,
    in a directory only this user can enter. It's written beside the file and renamed over
    it, so a reader never finds it empty or half written, and minted again every REFRESH
    seconds, so a token that lasts an hour always has 40 minutes left. Never in a command
    line, a log or an error message: a failure names the variable the command came from,
    and its exit status."""

    REFRESH = 20 * 60
    #: Older than this, it may expire during the next command: refuse to go on.
    MAX_AGE = 50 * 60

    def __init__(self, command: str, variable: str, *, prefix: str = "iki-live-") -> None:
        self.command = command
        self.variable = variable
        self.dir = Path(tempfile.mkdtemp(prefix=prefix))
        self.dir.chmod(0o700)
        self.path = self.dir / "token"
        self.minted = 0.0
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.refresher: threading.Thread | None = None

    def mint(self, *, attempts: int = 11, wait: float = 30) -> None:
        """A new token in the file, trying for five minutes before giving up."""
        with self.lock:
            status = None
            for attempt in range(attempts):
                result = subprocess.run(  # noqa: S603 - the caller's own command
                    shlex.split(self.command),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=120,
                )
                status = result.returncode
                token = result.stdout.strip().splitlines()[-1:] if result.returncode == 0 else []
                if token and TOKEN.fullmatch(token[0]):
                    write_private(self.path, token[0])
                    self.minted = time.monotonic()
                    return
                if attempt < attempts - 1:
                    time.sleep(wait)
            # Neither the output nor the error: either might hold a token.
            raise TokenError(f"{self.variable} printed no token (exit {status}) for five minutes")

    def start(self) -> None:
        """Mints the first token, and keeps minting new ones until stopped."""
        self.mint()
        self.refresher = threading.Thread(target=self._refresh, daemon=True)
        self.refresher.start()

    def _refresh(self) -> None:
        while not self.stopped.wait(self.REFRESH):
            try:
                self.mint()
            except TokenError:
                return

    def check(self) -> None:
        if time.monotonic() - self.minted > self.MAX_AGE:
            raise TokenError(
                f"the token is older than {self.MAX_AGE // 60} minutes and couldn't be minted again"
            )

    def read(self) -> str:
        self.check()
        return self.path.read_text()

    def stop(self) -> None:
        self.stopped.set()
        shutil.rmtree(self.dir, ignore_errors=True)


def write_private(path: Path, text: str) -> None:
    """text in path, readable by this user only, never seen half written: it's written
    beside it (mode 0600 from the start) and renamed over it."""
    fresh = path.with_name(path.name + ".new")
    descriptor = os.open(fresh, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as out:
        out.write(text)
    fresh.replace(path)


# --- The tools -----------------------------------------------------------------------------


class Tools:
    """Runs the command-line tools in the work directory, all as the one identity the token
    is, with the caller's own credentials and settings for each tool taken out of the
    environment (scrub) and Helm's repositories kept here. Subclasses add their cloud's
    CLI and OpenTofu, and what else the environment needs (extra_env)."""

    def __init__(self, root: Path, token: Token, env: dict[str, str], scrub: set[str]) -> None:
        self.root = root
        self.token = token
        base = {k: v for k, v in os.environ.items() if k not in scrub}
        base.update(
            KUBECONFIG=str(root / "kubeconfig"),
            HELM_CONFIG_HOME=str(root / "helm" / "config"),
            HELM_CACHE_HOME=str(root / "helm" / "cache"),
            HELM_DATA_HOME=str(root / "helm" / "data"),
            TF_IN_AUTOMATION="1",
            TF_INPUT="0",
            IKI_TEARDOWN_YES="1",
        )
        base.update(env)
        self.env = base

    def extra_env(self) -> dict[str, str]:
        """Variables set fresh for each command."""
        return {}

    def _env(self) -> dict[str, str]:
        self.token.check()
        return {**self.env, **self.extra_env()}

    def mint(self) -> None:
        self.token.mint()

    def stop(self) -> None:
        self.token.stop()

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
        # The child holds its own copy of the log's descriptor: this one closes now.
        with path.open("ab") as out:
            return subprocess.Popen(  # noqa: S603 - argv built here
                list(argv), stdout=out, stderr=subprocess.STDOUT, cwd=self.root, env=self._env()
            )

    def kubectl(self, *args: str, **kwargs: Any) -> str:
        return self.run("kubectl", *args, **kwargs).stdout

    def helm(self, *args: str, timeout: float = 1800) -> None:
        self.stream("helm", *args, log="helm.log", timeout=timeout)

    def exists(self, *args: str) -> bool:
        """Whether kubectl get finds it."""
        return self.run("kubectl", "get", *args, check=False).returncode == 0

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

    def server(self, *args: str) -> str:
        """An iki-server command in a web pod."""
        return self.kubectl(
            "-n", "iki", "exec", "deploy/iki-web", "-c", "web", "--", "iki-server", *args
        )


def tool(name: str) -> str:
    found = shutil.which(name)
    assert found, f"{name} is needed"
    return found


# --- Inventories ---------------------------------------------------------------------------


def write_sections(
    path: Path, sections: Mapping[str, set[str]], tail: list[str] | None = None
) -> None:
    """An inventory as text: each section's lines, sorted, under its name, then tail."""
    lines: list[str] = []
    for section, found in sections.items():
        lines += [f"=== {section}", *sorted(found), ""]
    path.write_text("\n".join([*lines, *(tail or [])]) + "\n")


def added(before: Mapping[str, set[str]], after: Mapping[str, set[str]]) -> dict[str, set[str]]:
    """What each section has now that it hadn't before."""
    return {
        s: found - before.get(s, set())
        for s, found in after.items()
        if found - before.get(s, set())
    }


def removed(before: Mapping[str, set[str]], after: Mapping[str, set[str]]) -> dict[str, set[str]]:
    """What each section had before that it hasn't now."""
    return {
        s: found - after.get(s, set()) for s, found in before.items() if found - after.get(s, set())
    }


# --- A certificate for an internal load balancer -------------------------------------------


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


# --- What the images are built from --------------------------------------------------------


def build_plugins(
    root: Path,
    more: Mapping[str, Path] | None = None,
    compute: list[dict[str, Any]] | None = None,
) -> Path:
    """The server's and the worker's plugins: iki/dna and iki/protein, their tools bound
    to the backend workers (as deploy/dev builds them), in root/plugins-build. A cloud's
    run may add plugins (more: each name's directory) and compute documents of its own
    (compute), such as the VM tier's templates."""
    out = root / "plugins-build"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    (out / "plugins.lock.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "iki/v1",
                "kind": "PluginLock",
                "plugins": [
                    *(
                        {"name": f"iki/{n}", "local": str(REPO / "plugins" / n), "state": "active"}
                        for n in PLUGINS
                    ),
                    *(
                        {"name": name, "local": str(path), "state": "active"}
                        for name, path in (more or {}).items()
                    ),
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
        *(compute or []),
    ]
    (out / "compute.yaml").write_text(yaml.safe_dump_all(documents))
    subprocess.run(  # noqa: S603 - argv built here
        [
            tool("uv"),
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
            "--test-only",
        ],
        check=True,
        cwd=REPO,
        timeout=1200,
        capture_output=True,
    )
    return out


def source_archive(root: Path, plugins_at: str) -> Path:
    """The repository at HEAD, and the plugins built for this deployment at plugins_at
    (build/<cloud>-live), gzipped: what the images are built from."""
    archive = root / "source.tar"
    subprocess.run(  # noqa: S603 - argv built here
        [tool("git"), "-C", str(REPO), "archive", "--format=tar", "-o", str(archive), "HEAD"],
        check=True,
        timeout=300,
    )
    with tarfile.open(archive, "a") as tar:
        tar.add(root / "plugins-build", arcname=plugins_at)
    gz = root / "source.tar.gz"
    subprocess.run(["gzip", "-f", str(archive)], check=True, timeout=300)  # noqa: S603, S607
    return gz


# --- When standing up fails ----------------------------------------------------------------


def diagnose(
    tools: Tools, probe_image: str, namespaces: tuple[str, ...] = ("external-secrets", "iki")
) -> None:
    """What the cluster says, in logs/diagnose.log, when standing up fails: once it's torn
    down, nothing tells why a pod never started. Then whether a pod resolves public names
    and reaches them, from probe_image (a postgres image the cluster may pull)."""
    if not (tools.root / "kubeconfig").exists():
        return
    commands: list[tuple[str, ...]] = [
        ("get", "nodes", "-o", "wide"),
        ("get", "pods", "-A", "-o", "wide"),
        ("get", "events", "-A", "--sort-by=.lastTimestamp"),
        *(("describe", "pods", "-n", n) for n in namespaces),
        (
            "-n",
            "default",
            "run",
            "iki-probe",
            f"--image={probe_image}",
            "--restart=Never",
            "--command",
            "--",
            "bash",
            "-c",
            "getent ahosts ghcr.io; getent ahosts github.com;"
            " timeout 10 bash -c '</dev/tcp/ghcr.io/443' && echo ghcr.io:443 reached",
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
    ]
    for args in commands:
        try:
            tools.stream("kubectl", *args, log="diagnose.log", timeout=180)
        except Exception as error:  # whatever it says, the teardown goes on
            sys.stdout.write(f"kubectl {' '.join(args[:4])}: {error!r}\n")


# --- Iki, through a port-forward -----------------------------------------------------------


class Iki:
    """HTTP to a web pod through kubectl port-forward, as a browser at publicUrl would
    send it: the Host it names, HTTPS as the load balancer forwards it. Iki's cookies are
    __Host- (Secure), so they're kept here and sent by hand over the forward's plain HTTP."""

    def __init__(self, port: int, host: str) -> None:
        self.port = port
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


def stop(process: subprocess.Popen[bytes], grace: float = 30) -> None:
    """Ends a process this run started, and reaps it: one terminated but never waited for
    is still running as far as Python knows, and pytest fails the run over its warning."""
    process.terminate()
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=30)


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


def wait_for_secrets(tools: Tools, names: tuple[str, ...], within: float = 300) -> None:
    """Until External Secrets has made each Secret in namespace iki."""
    for name in names:
        until(f"Secret {name}", lambda n=name: tools.exists("-n", "iki", "secret", n), within)  # type: ignore[misc]


def forward_to_web(tools: Tools, forwards: list[subprocess.Popen[bytes]], host: str) -> Iki:
    """A port-forward to a ready web pod, with no one signed in."""
    pods = json.loads(
        tools.kubectl(
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
    forwards.append(
        tools.popen(
            "kubectl",
            "-n",
            "iki",
            "port-forward",
            f"pod/{ready[0]}",
            f"{port}:8000",
            log="port-forward.log",
        )
    )

    def listening() -> bool:
        with socket.socket() as probe:
            return probe.connect_ex(("127.0.0.1", port)) == 0

    until("the port-forward", listening, 30, every=0.5)
    return Iki(port, host)


def sign_in_admin(tools: Tools, iki: Iki, label: str) -> tuple[Iki, str]:
    """The same, signed in as an Org Admin in a team of their own, and that team's ID. The
    deployment has no identity provider, so the session is made in a web pod, as Django's
    test client makes one (force_login): real sign-in isn't exercised here."""
    email = f"{label.lower().replace(' ', '-')}-{secrets.token_hex(4)}@example.com"
    made = tools.shell(f"""
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
user = User.objects.create_user({email!r}, {label!r})
OrgRoleAssignment.objects.create(user=user, role=OrgRole.ORG_ADMIN)
team, _ = Team.objects.get_or_create(name={label!r})
TeamMembership.objects.get_or_create(team=team, user=user)
client = Client()
client.force_login(user)
name = settings.SESSION_COOKIE_NAME
print(json.dumps({{"name": name, "value": client.cookies[name].value, "team": str(team.id)}}))
""")
    session = json.loads(made.strip().splitlines()[-1])
    signed_in = Iki(iki.port, iki.host)
    signed_in.cookies[session["name"]] = session["value"]
    return signed_in, str(session["team"])


def make_project(admin: Iki, team: str) -> dict[str, Any]:
    created = admin.api("POST", "/projects", {"team": team, "name": "Live check"})
    assert created.status_code == 201, created.text[:500]
    found: dict[str, Any] = created.json()
    assert found["my_role"] == "admin"
    return found


# --- The checks both clouds make -----------------------------------------------------------


def check_ready(iki: Iki) -> None:
    alive = iki.request("GET", "/healthz/live")
    assert alive.status_code == 200, alive.text
    ready = iki.request("GET", "/healthz/ready")
    assert ready.json() == {
        "status": "ok",
        "checks": {"database": "ok", "migrations": "ok", "database_role": "ok"},
    }


def database_connection(tools: Tools) -> dict[str, Any]:
    """The web pod's database connection: TLS, its version, the server's address, sslmode."""
    found: dict[str, Any] = json.loads(
        tools.shell("""
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
    return found


def check_setup_link(tools: Tools, iki: Iki, host: str) -> None:
    out = tools.server("setup_link")
    match = re.search(rf"https://{re.escape(host)}/setup/#(\S+)", out)
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


def check_project_and_entity(admin: Iki, project: dict[str, Any]) -> None:
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


def check_file_round_trip(
    admin: Iki,
    project: dict[str, Any],
    upload_url: Callable[[str], None],
    download_url: Callable[[str], None],
) -> None:
    """A file of a few parts goes up through the URLs Iki signs and comes down whole;
    upload_url and download_url check each URL is the cloud's, signed as it should be."""
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
        upload_url(part["url"])
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
    download_url(link)
    downloaded = requests.get(link, timeout=120)
    assert downloaded.status_code == 200 and hashlib.sha256(downloaded.content).hexdigest() == sha


def check_backend_run(admin: Iki, project: dict[str, Any]) -> None:
    """A backend worker computes a protein's properties. The runs pool starts at no nodes:
    the autoscaler adds one for the worker first."""
    made = admin.api(
        "POST",
        "/entities",
        {"type": "iki/protein.Protein", "project": project["id"], "values": {"sequence": HBB}},
    )
    protein = made.json()
    launched = admin.api(
        "POST",
        f"/actions/{ACTION}/runs",
        {"project": project["id"], "inputs": {"protein": protein["id"]}, "params": {}},
    )
    assert launched.status_code == 201, launched.text[:500]
    run_id = launched.json()["id"]
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


def check_setup_storage(tools: Tools) -> None:
    checked = tools.run(
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


# --- The deployment's life -----------------------------------------------------------------


@dataclass
class Live:
    """A live test's deployment, from its first inventory to its last. A cloud's subclass
    says how to list the account, stand up, tear down, and price the run."""

    cloud: str
    name: str
    workdir: Path
    tools: Tools
    #: The suite's version, recorded with a passing run: bumped when what it checks changes.
    suite_version: str
    before: Any = None
    after: Any = None
    torn_down: bool = False
    torn_down_checked: bool = False
    team: str = ""
    cost_usd: float | None = None
    forwards: list[subprocess.Popen[bytes]] = field(default_factory=list)
    teardown_errors: list[str] = field(default_factory=list)
    #: What tear_down found: its steps' errors, then what's left or gone.
    teardown_problems: list[str] = field(default_factory=list)
    #: The most a run may take before tearing down starts, in seconds (None: no cap). At the
    #: cap, whatever runs is stopped (CapReachedError), the checks left fail, and the last, which
    #: tears down, runs.
    cap_seconds: float | None = None

    # What each cloud says.

    def inventory(self) -> Any:
        """Everything in the account the test could leave behind; it has write(path)."""
        raise NotImplementedError

    def preconditions(self) -> None:
        """Refuses to start where applying would change what isn't the test's."""
        raise NotImplementedError

    def stand_up(self) -> None:
        raise NotImplementedError

    def diagnose(self) -> None:
        raise NotImplementedError

    def teardown_steps(self) -> list[tuple[str, Callable[[], object]]]:
        raise NotImplementedError

    def leftovers(self, before: Any, after: Any) -> list[str]:
        raise NotImplementedError

    def cost_report(self, start: datetime.datetime) -> str:
        """The run's cost at list prices, as text; sets cost_usd."""
        raise NotImplementedError

    # The same for every cloud.

    def tear_down(self) -> list[str]:
        """Deletes everything, once, and lists what's left that shouldn't be, and what's
        gone that should still be there: the same each time it's asked."""
        if self.torn_down:
            return list(self.teardown_problems)
        self.torn_down = True
        _stop_cap()  # tearing down is never cut short
        for forward in self.forwards:
            stop(forward)
        for what, step in [("a fresh token", self.tools.mint), *self.teardown_steps()]:
            try:
                step()
            except Exception as error:
                self.teardown_errors.append(f"{what}: {error}")
        try:
            self.after = self.inventory()
        except Exception as error:
            # Never taken as nothing left: what's in the account is unknown.
            self.teardown_problems = [
                *self.teardown_errors,
                f"listing the account after tearing down failed, so what's left or gone is "
                f"unknown: look at it by hand ({self.cloud}-before.txt has what was there): "
                f"{error}",
            ]
            return list(self.teardown_problems)
        self.after.write(self.workdir / f"{self.cloud}-after.txt")
        problems = self.leftovers(self.before, self.after) if self.before else []
        self.teardown_problems = [*self.teardown_errors, *problems]
        return list(self.teardown_problems)


class CapReachedError(Exception):
    """The run reached its cap (Live.cap_seconds): tear down now."""


#: Set when a run reaches its cap: a conftest's pytest_runtest_setup fails the checks left
#: (fail_if_capped), all but the one that tears down.
CAPPED = threading.Event()


def _start_cap(seconds: float | None) -> None:
    if not seconds:
        return

    def reached(signum: int, frame: object) -> None:
        CAPPED.set()
        signal.alarm(0)
        raise CapReachedError(f"the run reached its cap of {seconds / 3600:g} hours: tearing down")

    signal.signal(signal.SIGALRM, reached)
    signal.alarm(max(1, round(seconds)))


def _stop_cap() -> None:
    signal.alarm(0)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)


def fail_if_capped(item: pytest.Item) -> None:
    """For a conftest's pytest_runtest_setup: once the cap is reached, only the check that
    tears down still runs."""
    if CAPPED.is_set() and "tearing_down" not in item.name:
        pytest.fail("the run reached its cap: tearing down instead", pytrace=False)


#: Each live test's outcome, by its node ID: the module fixture reads them as it ends.
OUTCOMES = pytest.StashKey[dict[str, str]]()


def record_outcome(item: pytest.Item, report: pytest.TestReport) -> None:
    """For a conftest's pytest_runtest_makereport: a test's call outcome, or its setup's
    if that failed or skipped it."""
    outcomes = item.session.stash.setdefault(OUTCOMES, {})
    # A setup that failed or skipped, a call, or a teardown that failed after it.
    if (report.when == "call" or report.outcome != "passed") and (
        report.when != "setup" or item.nodeid not in outcomes
    ):
        outcomes[item.nodeid] = report.outcome


def lifecycle[D: Live](d: D, request: pytest.FixtureRequest) -> Iterator[D]:
    """The module fixture's body: the first inventory and the preconditions, then standing
    up (diagnosing first if it fails), the tests, and always tearing down, the run's
    summary and cost, and a record of the run in deploy/live-runs.json if every test of
    the module passed, the teardown left nothing, and the tree was clean and unchanged
    from start to end."""
    d.workdir.mkdir(parents=True, exist_ok=True)
    tree = live_runs.clean_head(REPO)
    try:
        d.tools.token.start()
        d.before = d.inventory()
        d.before.write(d.workdir / f"{d.cloud}-before.txt")
        d.preconditions()
    except BaseException:
        d.tools.stop()
        raise
    started = datetime.datetime.now(datetime.UTC)
    (d.workdir / f"{d.cloud}-window.json").write_text(json.dumps({"start": started.isoformat()}))
    CAPPED.clear()
    try:
        _start_cap(d.cap_seconds)
        try:
            d.stand_up()
        except BaseException:
            d.diagnose()
            raise
        yield d
    finally:
        _stop_cap()
        problems = d.tear_down()
        up = datetime.datetime.now(datetime.UTC) - started
        (d.workdir / f"{d.cloud}-summary.txt").write_text(
            f"resources up for {up}\n" + "\n".join(problems)
        )
        try:
            sys.stdout.write("\n" + d.cost_report(started))
        except Exception as error:
            sys.stdout.write(f"\nThe cost report failed: {error!r}\n")
        d.tools.stop()
        if problems and not d.torn_down_checked:
            pytest.fail("tearing down left:\n" + "\n".join(problems))
        if not problems:
            _record(d, request, tree, started, up)


def _record(
    d: Live,
    request: pytest.FixtureRequest,
    tree: str | None,
    started: datetime.datetime,
    up: datetime.timedelta,
) -> None:
    """Appends the run to deploy/live-runs.json if it passed whole, of a clean tree."""
    module = request.node.obj
    tests = [i for i in request.session.items if getattr(i, "module", None) is module]
    defined = {n for n in dir(module) if n.startswith("test_")}
    outcomes = request.session.stash.get(OUTCOMES, {})
    passed = [i.name for i in tests if outcomes.get(i.nodeid) == "passed"]
    why_not = None
    if tree is None:
        why_not = "the tree had changes when the run began"
    elif live_runs.clean_head(REPO) != tree:
        why_not = "the tree changed, or HEAD moved, during the run"
    elif set(passed) != defined:
        missing = sorted(defined - set(passed))
        why_not = f"not every check ran and passed: {', '.join(missing)}"
    if why_not:
        sys.stdout.write(f"\nNot recorded in deploy/live-runs.json: {why_not}.\n")
        return
    entry = live_runs.Entry(
        cloud=d.cloud,
        commit=tree,  # type: ignore[arg-type]
        started=started.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        duration_seconds=round(up.total_seconds()),
        cost_usd=None if d.cost_usd is None else round(d.cost_usd, 2),
        suite_version=d.suite_version,
        checks=sorted(passed),
    )
    live_runs.append(entry, live_runs.RECORD)
    sys.stdout.write(
        f"\nRecorded in deploy/live-runs.json: {d.cloud} at {tree}. Commit it: it's what make "
        "live-due and the release gate read.\n"
    )
