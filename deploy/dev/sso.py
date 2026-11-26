"""Sets up sign-in on the dev cluster through the real wizard and Keycloak.

    uv run python -m deploy.dev.sso                     # set up SSO, bob becomes admin
    uv run python -m deploy.dev.sso --check             # exercise sign-in and product scenarios
    uv run python -m deploy.dev.sso --check --fixtures  # also exercise the dev-ci overlay

It drives Iki like a browser: opens a one-time setup link from
`iki-server setup_link`, fills in the wizard, signs in on Keycloak's login
page, and finishes setup. Connections to Iki (localhost:8000) and Keycloak
(keycloak.iki.localhost:8080) go to the given local ports, so it works with
Tilt's port-forwards or with e2e.sh's own.
"""

import argparse
import base64
import hashlib
import hmac
import html
import http.cookiejar
import json
import math
import re
import secrets
import struct
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit
from uuid import UUID

import requests
import urllib3.util.connection
from examples.dose_response import public_plate, tutorial

IKI = "http://localhost:8000"
#: The web workload to exec into and read logs from (``--web``).
WEB = {"target": "deploy/iki-web"}
ISSUER = "http://keycloak.iki.localhost:8080/realms/iki"
SAML_METADATA = f"{ISSUER}/protocol/saml/descriptor"
#: Mailpit's API (``--mailpit-port``), where the dev cluster's email lands.
MAILPIT = {"url": "http://127.0.0.1:8025"}
#: The dev webhook receiver (``--webhook-port``), which lists what it was sent.
RECEIVER = {"url": "http://127.0.0.1:8090"}
#: Its address inside the cluster, where task runners send.
RECEIVER_IN_CLUSTER = "http://webhook-receiver.iki.svc:8080"
#: The fake LIMS's list of the requests it took (Tilt's port-forward).
LIMS = {"url": "http://127.0.0.1:8091"}
FOLDCLOUD_REQUESTS = {"url": "http://127.0.0.1:8092/requests"}
CONTEXT = "kind-iki"
NAMESPACE = "iki"


def fail(message: str) -> NoReturn:
    sys.stderr.write(f"\033[1;31mFAIL\033[0m {message}\n")
    sys.exit(1)


def ok(message: str) -> None:
    sys.stdout.write(f"\033[1;32m  ok\033[0m {message}\n")


def kubectl(*args: str, stdin: str | None = None) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["kubectl", "--context", CONTEXT, "-n", NAMESPACE, *args],  # noqa: S607
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def secret(name: str, key: str) -> str:
    raw = kubectl("get", "secret", name, "-o", f"jsonpath={{.data.{key}}}")
    return base64.b64decode(raw).decode()


def run_maintenance() -> None:
    """Run the deployed CronJob now. Its TTL cleans up each seed run's Job and pods."""
    name = f"iki-maintenance-seed-{secrets.token_hex(6)}"
    template = json.loads(
        kubectl(
            "create",
            "job",
            name,
            "--from=cronjob/iki-maintenance",
            "--dry-run=client",
            "-o",
            "json",
        )
    )
    container = next(
        c for c in template["spec"]["template"]["spec"]["containers"] if c["name"] == "maintain"
    )
    # iki-server is the image ENTRYPOINT when Kubernetes doesn't override it. A shell in
    # the same container can still read its cgroup's high-water mark after the server exits.
    command = [*container.get("command", ["iki-server"]), *container["args"]]
    container["command"] = ["/bin/sh", "-ec"]
    container["args"] = [
        '''status=0
"$@" || status=$?
read -r peak < /sys/fs/cgroup/memory.peak
read -r limit < /sys/fs/cgroup/memory.max
printf '{"event":"maintenance_cgroup","peak":"%s","limit":"%s"}\\n' "$peak" "$limit"
exit "$status"''',
        "maintenance-cgroup",
        *command,
    ]
    kubectl("create", "-f", "-", stdin=json.dumps(template))
    deadline = time.monotonic() + 300
    reason = "still incomplete after five minutes"
    while time.monotonic() < deadline:
        job = json.loads(kubectl("get", f"job/{name}", "-o", "json", "--request-timeout=10s"))
        terminal = next(
            (
                c
                for c in job.get("status", {}).get("conditions", [])
                if c["type"] in ("Complete", "Failed") and c["status"] == "True"
            ),
            None,
        )
        if terminal:
            if terminal["type"] == "Complete":
                report_maintenance_memory(name)
                return
            reason = f"{terminal.get('reason', 'Failed')}: {terminal.get('message', '')}"
            break
        time.sleep(2)

    selector = f"job-name={name}"
    diagnostics = [f"Maintenance Job {name}: {reason}"]
    for args in (
        ("get", "pods", "-l", selector, "-o", "wide"),
        (
            "logs",
            "-l",
            selector,
            "-c",
            "maintain",
            "--prefix",
            "--tail=40",
            "--pod-running-timeout=5s",
        ),
    ):
        try:
            diagnostics.append(kubectl(*args, "--request-timeout=10s").strip())
        except RuntimeError as error:
            diagnostics.append(str(error))
    fail("\n".join(diagnostics))


def report_maintenance_memory(name: str) -> None:
    pod = kubectl(
        "get",
        "pods",
        "-l",
        f"job-name={name}",
        "--field-selector=status.phase=Succeeded",
        "-o",
        "name",
    ).strip()
    logs = kubectl("logs", pod, "-c", "maintain")
    events = [json.loads(line) for line in logs.splitlines() if line.startswith("{")]
    loaded = next(
        (
            e
            for e in events
            if e.get("logger") == "iki_spec.wasm"
            and e.get("event") == "Loaded precompiled iki-wasm"
        ),
        None,
    )
    cgroup = next((e for e in events if e.get("event") == "maintenance_cgroup"), None)
    if not loaded or not cgroup:
        fail(f"{pod} did not report its precompiled module and cgroup memory\n{logs}")
    if cgroup["limit"] != str(384 * 1024**2) or int(cgroup["peak"]) <= 0:
        fail(f"{pod} reported unexpected cgroup memory {cgroup}")
    ok(
        f"{pod} loaded precompiled iki-wasm for {loaded['wasm_target']} "
        f"(SHA-256 {loaded['wasm_sha256']}). "
        f"memory.peak={cgroup['peak']} bytes and memory.max={cgroup['limit']} bytes"
    )


def route(iki_port: int, keycloak_port: int) -> None:
    """Sends connections for Iki's and Keycloak's public URLs to local ports."""
    routes = {("localhost", 8000): iki_port, ("keycloak.iki.localhost", 8080): keycloak_port}
    original = urllib3.util.connection.create_connection

    def create_connection(address: tuple[str, int], *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if address in routes:
            address = ("127.0.0.1", routes[address])
        return original(address, *args, **kwargs)

    urllib3.util.connection.create_connection = create_connection


class BrowserCookies(requests.cookies.RequestsCookieJar):
    """Treats *.localhost as a secure context, as browsers do: Secure cookies
    that Keycloak sets over http are sent back over http."""

    def set_cookie(self, cookie: http.cookiejar.Cookie, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        domain = cookie.domain.lstrip(".")
        if domain.endswith(("localhost", "localhost.local")):
            cookie.secure = False
        super().set_cookie(cookie, *args, **kwargs)


def browser() -> requests.Session:
    session = requests.Session()
    session.cookies = BrowserCookies()
    return session


class Forms(HTMLParser):
    """Collects each form's action and inputs."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: v or "" for k, v in attrs}
        if tag == "form":
            self.forms.append(
                {"id": values.get("id", ""), "action": values.get("action", ""), "inputs": {}}
            )
        elif tag == "input" and self.forms and values.get("name"):
            self.forms[-1]["inputs"][values["name"]] = values.get("value", "")


def form(
    response: requests.Response, form_id: str | None = None, index: int = 0
) -> tuple[str, dict[str, str]]:
    parser = Forms()
    parser.feed(response.text)
    forms = [f for f in parser.forms if form_id is None or f["id"] == form_id]
    if not forms:
        which = f"form #{form_id}" if form_id else "form"
        fail(f"no {which} on {response.url} (HTTP {response.status_code})")
    chosen = forms[index]
    action = (
        urljoin(response.url, html.unescape(chosen["action"])) if chosen["action"] else response.url
    )
    return action, dict(chosen["inputs"])


def submit(
    session: requests.Session,
    page_url: str,
    data: dict[str, str] | None = None,
    *,
    form_id: str | None = None,
) -> requests.Response:
    page = session.get(page_url)
    if page.status_code != 200:
        fail(f"GET {page_url} returned {page.status_code}")
    action, inputs = form(page, form_id)
    return session.post(action, data={**inputs, **(data or {})}, headers={"Origin": IKI})


def keycloak_sign_in(
    session: requests.Session, response: requests.Response, username: str
) -> requests.Response:
    """On Keycloak's login page, signs in and follows the redirects back to Iki.

    For SAML, Keycloak answers with a page whose script posts the SAMLResponse
    to Iki; this posts that form the way the script would.
    """
    if not response.url.startswith(ISSUER):
        fail(f"expected Keycloak's login page, got {response.url}")
    action, inputs = form(response, "kc-form-login")
    password = secret("keycloak-dev", "IKI_DEV_USER_PASSWORD")
    response = session.post(action, data={**inputs, "username": username, "password": password})
    if response.url.startswith(ISSUER) and "SAMLResponse" in response.text:
        action, inputs = form(response)
        response = session.post(action, data=inputs)
    return response


def setup_link() -> str:
    try:
        out = kubectl("exec", WEB["target"], "-c", "web", "--", "iki-server", "setup_link")
    except RuntimeError as error:
        if "already set up" not in str(error):
            raise
        out = kubectl(
            "exec", WEB["target"], "-c", "web", "--", "iki-server", "setup_link", "--recover"
        )
    match = re.search(r"(http://localhost:8000/setup/)#(\S+)", out)
    if not match:
        fail(f"setup_link printed no link: {out}")
    return match.group(2)


def set_up(admin: str, protocol: str = "oidc") -> requests.Session:
    """Runs the wizard; returns the admin's signed-in browser."""
    session = browser()
    response = submit(session, f"{IKI}/setup/", {"token": setup_link()}, form_id="setup-token-form")
    if urlsplit(response.url).path == "/setup/organization/":
        response = submit(session, response.url, {"organization_name": "Development"})
    if urlsplit(response.url).path != "/setup/identity-provider/":
        fail(f"wizard went to {response.url} after the setup link (HTTP {response.status_code})")
    if protocol == "saml":
        details = {"display_name": "Keycloak", "metadata_url": SAML_METADATA}
    else:
        details = {
            "display_name": "Keycloak",
            "issuer": ISSUER,
            "client_id": "iki-web",
            "client_secret": secret("keycloak-dev", "IKI_DEV_OIDC_CLIENT_SECRET"),
        }
    response = submit(
        session,
        f"{IKI}/setup/identity-provider/?protocol={protocol}",
        {"protocol": protocol, **details},
    )
    if urlsplit(response.url).path != "/setup/verify/":
        errors = re.findall(r'class="field-error"[^>]*>([^<]+)', response.text)
        fail(f"identity provider step failed: {errors or response.status_code}")
    if protocol == "saml":
        ok("wizard read Keycloak's SAML metadata in the cluster and pinned its certificate")
    else:
        ok("wizard checked Keycloak's discovery document from inside the cluster")
    response = keycloak_sign_in(session, submit(session, response.url), admin)
    if urlsplit(response.url).path != "/setup/complete/":
        fail(f"after signing in at Keycloak, landed on {response.url}")
    response = submit(session, response.url)
    if urlsplit(response.url).path != "/setup/done/" or "Iki is ready" not in response.text:
        fail(f"finishing setup landed on {response.url}")
    ok(f"setup finished through Keycloak ({protocol.upper()}); {admin} is an Org Admin")
    return session


def post(
    session: requests.Session, url: str, data: dict[str, str | list[str]]
) -> requests.Response:
    """Posts like a page's form: CSRF token from the page at ``url``."""
    _, inputs = form(session.get(url), index=0)
    token = inputs["csrfmiddlewaretoken"]
    return session.post(url, data={"csrfmiddlewaretoken": token, **data}, headers={"Origin": IKI})


def teams_and_projects(
    admin: requests.Session, member: requests.Session, email: str
) -> dict[str, Any]:
    """The Org Admin creates a team and adds someone; they create a project in it."""
    name = f"E2E {int(time.time())}"
    response = post(admin, f"{IKI}/teams/", {"name": name})
    team_path = urlsplit(response.url).path
    if not team_path.startswith("/teams/e2e-"):
        fail(f"creating a team landed on {response.url}")
    response = post(admin, f"{IKI}{team_path}", {"person": email})
    if "Added " not in response.text:
        fail(f"adding {email} to the team failed (HTTP {response.status_code})")
    ok("the Org Admin created a team and added a member")
    told(member, email, name)

    # Every run makes a team, so a long-lived cluster has many: look through them all.
    teams: list[dict[str, Any]] = []
    cursor = ""
    while True:
        after = f"&cursor={quote(cursor)}" if cursor else ""
        found = api_call(member, "GET", f"/teams?can_create_projects=true&limit=100{after}").json()
        teams += found["items"]
        cursor = found.get("next_cursor") or ""
        if not cursor:
            break
    team = next((t for t in teams if t["name"] == name), None)
    if team is None:
        fail("the new team isn't offered for new projects")
    created = api_call(member, "POST", "/projects", {"team": team["id"], "name": "Atlas"})
    if created.status_code != 201 or created.json()["my_role"] != "admin":
        fail(f"creating a project: HTTP {created.status_code} {created.text[:200]}")
    project: dict[str, Any] = created.json()
    if member.get(f"{IKI}/projects/{project['slug']}/").status_code != 200:
        fail("the project's page doesn't load")
    ok("the team member created a project and became its Admin")

    seen = api_call(admin, "GET", f"/projects/{project['id']}").json()
    if seen["can"]["read"] or not seen["can"]["manage_members"]:
        fail(f"the Org Admin's access to a project they're not in: {seen['can']}")
    ok("the Org Admin sees the project exists, but not its data")

    people = admin.get(f"{IKI}/admin/people/")
    me = re.search(r'href="(/admin/people/[0-9a-f-]+/)">Bob', people.text)
    if not me:
        fail("bob isn't on the people page")
    post(admin, f"{IKI}{me.group(1)}", {"roles": ["org_admin"]})  # as on a first run
    if admin.get(f"{IKI}/admin/audit/").status_code != 403:
        fail("an Org Admin without the Auditor role can read the audit log")
    post(admin, f"{IKI}{me.group(1)}", {"roles": ["org_admin", "auditor"]})
    log = admin.get(f"{IKI}/admin/audit/")
    if "Created a project: Atlas" not in log.text or "Granted an org role" not in log.text:
        fail(f"the audit log page lacks the team and project events (HTTP {log.status_code})")
    ok("an Auditor reads the team, project and role changes in the audit log")
    return project


def told(member: requests.Session, email: str, team: str) -> None:
    """Being added to a team tells the person: in Iki, live, and by email (a task)."""
    with member.get(
        f"{IKI}/api/v1/events", headers={"Origin": IKI}, stream=True, timeout=10
    ) as stream:
        if stream.headers.get("Content-Type", "").split(";")[0] != "text/event-stream":
            fail(f"live updates: HTTP {stream.status_code} {stream.headers.get('Content-Type')}")
        lines = stream.iter_lines()
        first = [next(lines).decode() for _ in range(4)]
    ready = next((line for line in first if line.startswith("data:")), "")
    if "event: ready" not in first or json.loads(ready[5:])["unread"] < 1:
        fail(f"the live-update stream didn't start with the unread count: {first}")
    inbox = api_call(member, "GET", "/notifications?unread=true").json()["items"]
    if not any(n["kind"] == "team.added" and team in n["title"] for n in inbox):
        fail(f"no notification about joining {team}: {inbox}")
    ok("the new member has a notification, and the live-update stream counts it")

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        messages = requests.get(f"{MAILPIT['url']}/api/v1/messages?limit=50", timeout=5).json()
        found = next(
            (
                m
                for m in messages["messages"]
                if team in m["Subject"] and any(t["Address"] == email for t in m["To"])
            ),
            None,
        )
        if found:
            break
        time.sleep(1)
    else:
        fail(f"no email to {email} about {team} reached Mailpit within a minute")
    headers = requests.get(
        f"{MAILPIT['url']}/api/v1/message/{found['ID']}/headers", timeout=5
    ).json()
    if headers.get("Auto-Submitted") != ["auto-generated"] or "List-Unsubscribe" not in headers:
        fail(f"the email lacks Auto-Submitted or List-Unsubscribe: {sorted(headers)}")
    ok("the task runner emailed it, marked automatic (no out-of-office replies)")


def identity_provider_mail(user: str) -> None:
    """Keycloak sends account email (setup, password resets) to Mailpit, as Iki does: an
    admin adding a test person can send them their setup email."""
    keycloak = ISSUER.removesuffix("/realms/iki")
    token = requests.post(
        f"{keycloak}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": secret("keycloak-dev", "KC_BOOTSTRAP_ADMIN_USERNAME"),
            "password": secret("keycloak-dev", "KC_BOOTSTRAP_ADMIN_PASSWORD"),
        },
        allow_redirects=False,
        timeout=10,
    )
    if token.status_code != 200:
        fail(f"Keycloak's admin token: HTTP {token.status_code} {token.text[:200]}")
    admin = {"Authorization": f"Bearer {token.json()['access_token']}"}
    found = requests.get(
        f"{keycloak}/admin/realms/iki/users",
        params={"username": user, "exact": "true"},
        headers=admin,
        allow_redirects=False,
        timeout=10,
    ).json()
    if len(found) != 1:
        fail(f"Keycloak has {len(found)} users named {user}")
    # Verifying an address that's verified already: harmless, should anyone follow it.
    sent = requests.put(
        f"{keycloak}/admin/realms/iki/users/{found[0]['id']}/execute-actions-email",
        params={"lifespan": 300},
        json=["VERIFY_EMAIL"],
        headers=admin,
        allow_redirects=False,
        timeout=30,
    )
    if sent.status_code != 204:
        fail(f"Keycloak couldn't send {user} an email: HTTP {sent.status_code} {sent.text[:200]}")
    email = f"{user}@iki.test"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        messages = requests.get(f"{MAILPIT['url']}/api/v1/messages?limit=50", timeout=5).json()
        if any(
            m["From"]["Address"] == "identity@iki.test"
            and any(t["Address"] == email for t in m["To"])
            for m in messages["messages"]
        ):
            break
        time.sleep(1)
    else:
        fail(f"Keycloak's email to {email} didn't reach Mailpit within a minute")
    ok(f"Keycloak emails {user} through Mailpit, from identity@iki.test")


def landed(session: requests.Session, response: requests.Response, user: str) -> None:
    """Signing in ends on the home page (the web app), signed in as ``user``."""
    if urlsplit(response.url).path != "/" or '<div id="root">' not in response.text:
        fail(f"signing in as {user} landed on {response.url} (HTTP {response.status_code})")
    me = session.get(f"{IKI}/api/v1/me")
    if me.status_code != 200 or not me.json()["name"].startswith(user.title()):
        fail(f"signed in as {user}, but /api/v1/me says HTTP {me.status_code}: {me.text[:200]}")


def sign_in_as(user: str) -> requests.Session:
    session = browser()
    action, inputs = form(session.get(f"{IKI}/login/"))
    response = keycloak_sign_in(
        session, session.post(action, data=inputs, headers={"Origin": IKI}), user
    )
    landed(session, response, user)
    return session


class Scim:
    """Plays the identity provider's SCIM client (Keycloak can't push SCIM)."""

    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/scim+json"}
        )

    def __call__(self, method: str, path: str, body: Any = None) -> Any:  # noqa: ANN401
        response = self.session.request(
            method, f"{IKI}/scim/v2{path}", json=body, allow_redirects=False
        )
        if response.status_code >= 300:
            fail(f"SCIM {method} {path}: HTTP {response.status_code} {response.text[:300]}")
        return response.json() if response.content else None

    def user(self, email: str) -> str:
        """Finds or creates someone, as Entra ID and Okta do."""
        found = self("GET", f'/Users?filter=userName eq "{email}"')["Resources"]
        if found:
            return str(found[0]["id"])
        created = self(
            "POST",
            "/Users",
            {"userName": email, "emails": [{"value": email, "primary": True}], "active": True},
        )
        return str(created["id"])


def provisioning(admin: requests.Session) -> None:
    """SCIM: the Org Admin turns it on, the IdP pushes people and a group, access follows."""
    page = post(admin, f"{IKI}/admin/sign-in/", {"action": "create_token"})
    token = re.search(r'id="scim-token">([^<]+)<', page.text)
    if not token:
        fail(f"creating a SCIM token showed no token (HTTP {page.status_code})")
    scim = Scim(token.group(1))
    alice_id = scim.user("alice@iki.test")
    scim.user("bob@iki.test")
    scim.user("carol@iki.test")
    ok("the Org Admin turned on SCIM and provisioned all three dev users")

    name = f"E2E Chemistry {int(time.time())}"
    group = scim("POST", "/Groups", {"displayName": name, "members": [{"value": alice_id}]})
    response = post(
        admin, f"{IKI}/admin/sign-in/groups/{group['id']}/", {"action": "link", "team": "new"}
    )
    if (
        "Members come from the identity provider group" not in response.text
        or "alice@iki.test" not in response.text
    ):
        fail(f"linking {name} to a new team landed on {response.url} (HTTP {response.status_code})")
    ok("a pushed group, linked to a team, decides who is on it")

    alice = sign_in_as("alice")
    scim(
        "PATCH",
        f"/Users/{alice_id}",
        {"Operations": [{"op": "Replace", "path": "active", "value": "False"}]},
    )
    if urlsplit(alice.get(f"{IKI}/").url).path != "/login/":
        fail("alice's session survived SCIM deactivation")
    ok("deactivating alice over SCIM ended her session at once")
    scim(
        "PATCH",
        f"/Users/{alice_id}",
        {"Operations": [{"op": "Replace", "path": "active", "value": "True"}]},
    )
    scim("DELETE", f"/Groups/{group['id']}")
    sign_in_as("alice")
    ok("reactivated over SCIM, alice signs in again")


def api_call(
    session: requests.Session,
    method: str,
    path: str,
    body: Any = None,  # noqa: ANN401
    extra: dict[str, str] | None = None,
) -> requests.Response:
    """Calls /api/v1 like Iki's pages do: the session, plus the page's CSRF token."""
    headers = {"Origin": IKI, **(extra or {})}
    if method != "GET":
        page = session.get(f"{IKI}/projects/")
        token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)
        if not token:
            fail("pages don't carry the CSRF token for API calls")
        headers["X-CSRFToken"] = token.group(1)
    return session.request(method, f"{IKI}/api/v1{path}", json=body, headers=headers)


def entry_of(entity: dict[str, Any]) -> str:
    """What a reference to an entity holds: for a type with an identity, its registry
    entry's ID (ADR 0115), which every project's record of it shares; otherwise its own."""
    entry = entity.get("entry")
    return str(entry["id"] if entry else entity["id"])


def record(session: requests.Session, entity_id: str) -> dict[str, Any]:
    """A record, by its own ID: an entry's HRID can name several projects' records."""
    got = api_call(session, "GET", f"/entities/{entity_id}")
    if got.status_code != 200:
        fail(f"reading {entity_id} answered HTTP {got.status_code}: {got.text[:300]}")
    found: dict[str, Any] = got.json()
    return found


def rest_api(admin: requests.Session) -> None:
    """The REST API: problems, CSRF, rate-limit headers, service accounts and project access."""
    anonymous = browser().get(f"{IKI}/api/v1/me")
    if anonymous.status_code != 401 or anonymous.headers.get("Content-Type") != (
        "application/problem+json"
    ):
        fail(f"an anonymous API call got HTTP {anonymous.status_code}, not a 401 problem")
    me = admin.get(f"{IKI}/api/v1/me")
    if me.status_code != 200 or "org_admin" not in me.json()["org_roles"]:
        fail(f"/api/v1/me answered HTTP {me.status_code}: {me.text[:200]}")
    if not me.headers.get("RateLimit", "").startswith('"api";r='):
        fail(f"API responses lack RateLimit headers: {dict(me.headers)}")
    ok("the API answers problems to strangers and reports rate limits to callers")

    refused = admin.post(
        f"{IKI}/api/v1/service-accounts", json={"name": "x"}, headers={"Origin": IKI}
    )
    if refused.status_code != 403 or refused.json().get("code") != "csrf_failed":
        fail(f"an API change without the CSRF token got HTTP {refused.status_code}")
    name = f"E2E pipeline {int(time.time())}"
    created = api_call(admin, "POST", "/service-accounts", {"name": name, "description": "e2e"})
    if created.status_code != 201 or not created.json()["email"].endswith(
        "@service-accounts.iki.invalid"
    ):
        fail(f"creating a service account answered HTTP {created.status_code}: {created.text}")
    account = created.json()
    ok("an Org Admin created a service account over the API, with the CSRF token")

    alice = sign_in_as("alice")
    mine = api_call(alice, "GET", "/projects?member=true&limit=100").json()["items"]
    atlas = [p for p in mine if p["name"] == "Atlas" and p["my_role"] == "admin"]
    if not atlas:
        fail("alice's Atlas project isn't listed as hers")
    project_id = atlas[-1]["id"]
    # EICAR's harmless antivirus test file exercises the deployed clamd and its signatures.
    eicar = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}" + b"$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    held = upload(
        alice, atlas[-1], "eicar.com", eicar, "application/octet-stream", expected_scan="infected"
    )
    evidence = api_call(alice, "GET", f"/files/{held}").json()["scan"]
    if (
        not evidence["scanner"].startswith("ClamAV ")
        or not evidence["signature_version"]
        or "eicar" not in evidence["reason"].lower()
    ):
        fail(f"EICAR has incomplete scanner evidence: {evidence}")
    refused_url = api_call(alice, "GET", f"/files/{held}/content-url")
    if refused_url.status_code != 409 or refused_url.json().get("code") != "file_quarantined":
        fail(f"a quarantined file's content URL answered HTTP {refused_url.status_code}")
    ok("ClamAV detected the EICAR test file and Iki quarantined it")

    granted = api_call(
        alice,
        "PUT",
        f"/projects/{project_id}/members/users/{account['id']}",
        {"role": "runner"},
    )
    if granted.status_code != 200 or granted.json()["role"] != "runner":
        fail(f"granting the service account a role answered HTTP {granted.status_code}")
    ok("alice, a project Admin, gave the service account a role in her project")

    org_admin = api_call(
        admin, "PUT", f"/users/{account['id']}/org-roles", {"roles": ["org_admin"]}
    )
    if org_admin.status_code != 409:
        fail(f"a service account was made an Org Admin (HTTP {org_admin.status_code})")
    stopped = api_call(admin, "PATCH", f"/service-accounts/{account['id']}", {"active": False})
    if stopped.status_code != 200 or stopped.json()["active"] is not False:
        fail(f"deactivating the service account answered HTTP {stopped.status_code}")
    log = admin.get(f"{IKI}/admin/audit/")
    if "Created a service account" not in log.text:
        fail("the audit log lacks the service account")
    ok("service accounts can't be Org Admins; deactivating one is immediate and audited")


def received(path: str, event: str) -> dict[str, Any]:
    """The receiver's request to ``path`` carrying ``event``, within a minute."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        for request in requests.get(f"{RECEIVER['url']}/requests", timeout=5).json():
            if request["path"] == path and json.loads(request["body"])["type"] == event:
                found: dict[str, Any] = request
                return found
        time.sleep(1)
    fail(f"no {event} webhook reached the receiver at {path} within a minute")


def standard_webhooks_verify(secret: str, request: dict[str, Any]) -> bool:
    """Checks a Standard Webhooks signature, independently of Iki's own code."""
    headers = {k.lower(): v for k, v in request["headers"].items()}
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.{request['body']}"
    expected = base64.b64encode(hmac.digest(key, signed.encode(), "sha256")).decode()
    if abs(time.time() - int(headers["webhook-timestamp"])) > 300:
        return False
    return any(
        hmac.compare_digest(f"v1,{expected}", candidate)
        for candidate in headers["webhook-signature"].split()
    )


def webhooks(admin: requests.Session) -> None:
    """A project Admin's webhook: signed messages from the task runner, and the log of them."""
    alice = sign_in_as("alice")
    mine = api_call(alice, "GET", "/projects?member=true&limit=100").json()["items"]
    project = next(p for p in mine if p["name"] == "Atlas" and p["my_role"] == "admin")
    path = f"/e2e/{secrets.token_hex(8)}"
    created = api_call(
        alice,
        "POST",
        f"/projects/{project['id']}/webhooks",
        {
            "url": f"{RECEIVER_IN_CLUSTER}{path}",
            "description": "e2e",
            "events": ["project.updated"],
        },
    )
    if created.status_code != 201 or not created.json()["secret"].startswith("whsec_"):
        fail(f"adding a webhook answered HTTP {created.status_code}: {created.text[:300]}")
    hook = created.json()
    ping = received(path, "ping")
    if not standard_webhooks_verify(hook["secret"], ping):
        fail(f"the ping's signature doesn't verify: {ping['headers']}")
    if standard_webhooks_verify(hook["secret"], {**ping, "body": ping["body"] + " "}):
        fail("a changed body still verified")
    ok("adding a webhook sends a ping from the task runner, signed as Standard Webhooks")

    renamed = api_call(alice, "PATCH", f"/projects/{project['id']}", {"description": f"e2e {path}"})
    if renamed.status_code != 200:
        fail(f"changing the project answered HTTP {renamed.status_code}")
    update = received(path, "project.updated")
    body = json.loads(update["body"])
    if (
        not standard_webhooks_verify(hook["secret"], update)
        or body["data"]["project"]["id"] != project["id"]
    ):
        fail(f"the project.updated message is wrong: {update}")
    deliveries = api_call(alice, "GET", f"/webhooks/{hook['id']}/deliveries").json()["items"]
    if not deliveries or any(d["status"] == "failed" for d in deliveries):
        fail(f"the delivery log doesn't show the deliveries: {deliveries}")
    ok("changing the project sends project.updated; the delivery log shows both")

    listed = api_call(admin, "GET", "/project-webhooks?limit=100").json()["items"]
    seen = next((w for w in listed if w["id"] == hook["id"]), None)
    if not seen or seen["host"] != "webhook-receiver.iki.svc:8080" or "url" in seen:
        fail(f"the Org Admin's inventory doesn't show where it sends: {seen}")
    gone = api_call(alice, "DELETE", f"/webhooks/{hook['id']}")
    if gone.status_code != 204:
        fail(f"deleting the webhook answered HTTP {gone.status_code}")
    ok("Org Admins see where project webhooks send (the host only)")


def csrf_token(page: requests.Response) -> str:
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
    if not token:
        fail(f"no CSRF token on {page.url}")
    return token.group(1)


def page_post(
    session: requests.Session,
    url: str,
    data: dict[str, Any],
    **kw: Any,  # noqa: ANN401
) -> requests.Response:
    """Posts to ``url`` as its page's form would, with the page's CSRF token."""
    token = csrf_token(session.get(url))
    return session.post(
        url, data={"csrfmiddlewaretoken": token, **data}, headers={"Origin": IKI}, **kw
    )


def bearer_call(token: str, method: str, path: str, body: Any = None) -> requests.Response:  # noqa: ANN401
    return browser().request(
        method,
        f"{IKI}/api/v1{path}",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        allow_redirects=False,
    )


def cli_read_token(session: requests.Session, *, project: str | None = None) -> str:
    """Approve a read-only device token, optionally restricted to one project."""
    app = browser()
    started = app.post(
        f"{IKI}/oauth/device-authorization/", data={"client_id": "iki-cli", "scope": "read write"}
    )
    if started.status_code != 200:
        fail(f"device authorization answered HTTP {started.status_code}: {started.text}")
    device = started.json()
    entered = page_post(
        session, f"{IKI}/oauth/device/", {"user_code": device["user_code"]}, allow_redirects=False
    )
    if entered.status_code != 302:
        fail(f"entering the device code answered HTTP {entered.status_code}")
    confirm = urljoin(IKI, entered.headers["Location"])
    if "Iki CLI" not in session.get(confirm).text:
        fail("the device confirmation page doesn't name the CLI")
    choice = {"access": "chosen", "projects": project} if project else {"access": "all"}
    done = page_post(session, confirm, {"granted": ["read"], **choice})
    if done.status_code != 200:
        fail(f"approving the device answered HTTP {done.status_code}")
    time.sleep(device["interval"])
    tokens = app.post(
        f"{IKI}/oauth/token/",
        allow_redirects=False,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device["device_code"],
            "client_id": "iki-cli",
        },
    )
    if tokens.status_code != 200 or tokens.json().get("scope") != "read":
        fail(f"the device poll answered HTTP {tokens.status_code}: {tokens.text}")
    return str(tokens.json()["access_token"])


def oauth(admin: requests.Session) -> None:
    """OAuth 2.1: the CLI's device flow, a self-registered app with PKCE and approval,
    and a service account's client credentials."""
    app = browser()  # the client: no cookies of its own
    meta = app.get(f"{IKI}/.well-known/oauth-authorization-server").json()
    if meta.get("issuer") != IKI or meta.get("code_challenge_methods_supported") != ["S256"]:
        fail(f"unexpected authorization server metadata: {meta}")
    challenge = app.get(f"{IKI}/api/v1/me", headers={"Authorization": "Bearer nope"})
    if "resource_metadata=" not in challenge.headers.get("WWW-Authenticate", ""):
        fail(f"a bad token's 401 doesn't point at the metadata: {dict(challenge.headers)}")
    ok("clients discover the OAuth server from metadata and from a 401")

    # The CLI: device flow.
    alice = sign_in_as("alice")
    token = cli_read_token(alice)
    me = bearer_call(token, "GET", "/me")
    if me.status_code != 200 or me.json()["email"] != "alice@iki.test":
        fail(f"the CLI's token answered HTTP {me.status_code}: {me.text[:200]}")
    mine = bearer_call(token, "GET", "/projects?member=true&limit=100").json()["items"]
    atlas = next(p for p in mine if p["name"] == "Atlas" and p["my_role"] == "admin")
    write = bearer_call(token, "PATCH", f"/projects/{atlas['id']}", {"description": "x"})
    if write.status_code != 403 or write.json().get("code") != "insufficient_scope":
        fail(f"a read-only token changed a project (HTTP {write.status_code})")
    ok("alice signed the CLI in with a device code, allowing read only; it can't write")

    # A self-registered app: waits for approval, then authorization code with PKCE.
    redirect = "http://127.0.0.1:53682/callback"
    registered = app.post(
        f"{IKI}/oauth/register/",
        json={
            "client_name": f"E2E notebook {int(time.time())}",
            "redirect_uris": [redirect],
            "token_endpoint_auth_method": "none",
        },
    )
    if registered.status_code != 201:
        fail(f"registering a client answered HTTP {registered.status_code}: {registered.text}")
    client = registered.json()
    verifier = secrets.token_urlsafe(48)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    query = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": redirect,
        "scope": "read write",
        "state": "e2e",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    waiting = alice.get(f"{IKI}/oauth/authorize/", params=query)
    if waiting.status_code != 403:
        fail(f"an unapproved app's consent page answered HTTP {waiting.status_code}")
    apps = admin.get(f"{IKI}/admin/apps/")
    link = re.search(r'href="(/admin/apps/\d+/)">' + re.escape(client["client_name"]), apps.text)
    if not link:
        fail("the new app isn't on Admin → Apps")
    page_post(admin, urljoin(IKI, link.group(1)), {"action": "approve"})
    consent_page = alice.get(f"{IKI}/oauth/authorize/", params=query)
    if consent_page.status_code != 200:
        fail(f"the approved app's consent page answered HTTP {consent_page.status_code}")
    csp = consent_page.headers.get("Content-Security-Policy", "")
    form_action = next((d for d in csp.split(";") if d.strip().startswith("form-action")), "")
    if "http://127.0.0.1:53682" not in form_action.split():
        fail("the consent page's CSP doesn't allow the redirect back to the app")
    allowed = alice.post(
        f"{IKI}/oauth/authorize/",
        data={
            **query,
            "csrfmiddlewaretoken": csrf_token(consent_page),
            "allow": "Allow",
            "granted": ["read", "write"],
            "access": "chosen",
            "projects": [re.search(r'name="projects" value="([^"]+)"', consent_page.text)[1]],  # type: ignore[index]
        },
        headers={"Origin": IKI},
        allow_redirects=False,
    )
    location = urlsplit(allowed.headers.get("Location", ""))
    params = parse_qs(location.query)
    if allowed.status_code != 302 or "code" not in params or params.get("iss") != [IKI]:
        fail(f"allowing the app answered HTTP {allowed.status_code}: {allowed.headers}")
    exchanged = app.post(
        f"{IKI}/oauth/token/",
        allow_redirects=False,
        data={
            "grant_type": "authorization_code",
            "code": params["code"][0],
            "redirect_uri": redirect,
            "client_id": client["client_id"],
            "code_verifier": verifier,
        },
    )
    if exchanged.status_code != 200 or not exchanged.json().get("refresh_token"):
        fail(f"exchanging the code answered HTTP {exchanged.status_code}: {exchanged.text}")
    app_token = exchanged.json()["access_token"]
    if bearer_call(app_token, "GET", "/me").json().get("email") != "alice@iki.test":
        fail("the app's token isn't alice's")
    ok("a self-registered app waited for approval, then got alice's consent with PKCE")
    mcp(app_token)

    revoke = alice.get(f"{IKI}/account/apps/")
    consent_id = re.search(r'name="consent" value="(\d+)"', revoke.text)
    if not consent_id:
        fail("alice's authorized apps page lists nothing to revoke")
    page_post(alice, f"{IKI}/account/apps/", {"action": "revoke", "consent": consent_id[1]})
    if bearer_call(app_token, "GET", "/me").status_code != 401:
        fail("a revoked app's token still works")
    ok("revoking an app on Account → Apps signs it out at once")

    # A service account: client credentials.
    created = api_call(admin, "POST", "/service-accounts", {"name": f"E2E LIMS {int(time.time())}"})
    account = created.json()
    page = page_post(
        admin,
        f"{IKI}/admin/service-accounts/{account['id']}/",
        {"action": "create_credential", "name": "e2e", "scopes": ["read"]},
    )
    client_id = re.search(r'id="client-id">([^<]+)<', page.text)
    secret = re.search(r'id="client-secret">([^<]+)<', page.text)
    if not (client_id and secret):
        fail("creating service-account credentials didn't show them once")
    issued = app.post(
        f"{IKI}/oauth/token/",
        allow_redirects=False,
        data={"grant_type": "client_credentials"},
        auth=(client_id[1], html.unescape(secret[1])),
    )
    if issued.status_code != 200:
        fail(f"client credentials answered HTTP {issued.status_code}: {issued.text}")
    robot = bearer_call(issued.json()["access_token"], "GET", "/me").json()
    if robot.get("id") != account["id"]:
        fail(f"the credential's token isn't the service account's: {robot}")
    ok("a service account got a token with client credentials")

    # The same service account acting for alice, by her delegation (ADR 0073).
    page = page_post(
        admin,
        f"{IKI}/admin/service-accounts/{account['id']}/",
        {
            "action": "create_credential",
            "name": "e2e acting",
            "scopes": ["read"],
            "acts_for_people": "on",
        },
    )
    acting_id = re.search(r'id="client-id">([^<]+)<', page.text)
    acting_secret = re.search(r'id="client-secret">([^<]+)<', page.text)
    if not (acting_id and acting_secret):
        fail("creating a credential that acts for people didn't show it once")
    listed = alice.get(f"{IKI}/account/apps/delegate/").text
    choose = re.search(
        re.escape(html.escape(account["name"])) + r'.*?href="(/account/apps/delegate/\d+/)"',
        listed,
        re.DOTALL,
    )
    if not choose:
        fail("the credential isn't offered on Account → Apps → Let an agent act for you")
    form_url = urljoin(IKI, choose[1])
    latest = re.search(r'type="date"[^>]*max="([0-9-]+)"', alice.get(form_url).text)
    if not latest:
        fail("the delegation form doesn't bound its date")
    delegated = page_post(
        alice,
        form_url,
        {"granted": ["read"], "access": "all", "until": latest[1]},
        allow_redirects=False,
    )
    grant = re.search(r"#grant-([0-9a-f-]+)$", delegated.headers.get("Location", ""))
    if delegated.status_code != 302 or not grant:
        fail(f"delegating answered HTTP {delegated.status_code}: {delegated.text[:300]}")
    acting = app.post(
        f"{IKI}/oauth/token/",
        allow_redirects=False,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": grant[1],
            "subject_token_type": "urn:ikisci:params:oauth:token-type:delegation",
        },
        auth=(acting_id[1], html.unescape(acting_secret[1])),
    )
    if acting.status_code != 200 or acting.json().get("act") != {"sub": acting_id[1]}:
        fail(f"the token exchange answered HTTP {acting.status_code}: {acting.text}")
    acting_token = acting.json()["access_token"]
    acted = bearer_call(acting_token, "GET", "/me").json()
    if (
        acted.get("email") != "alice@iki.test"
        or (acted.get("delegation") or {}).get("service_account", {}).get("id") != account["id"]
    ):
        fail(f"the exchanged token doesn't act for alice through the agent: {acted}")
    ok("alice let the service account's credential act for her; it exchanged her delegation")

    api_call(admin, "PATCH", f"/service-accounts/{account['id']}", {"active": False})
    if bearer_call(issued.json()["access_token"], "GET", "/me").status_code != 401:
        fail("a deactivated service account's token still works")
    if bearer_call(acting_token, "GET", "/me").status_code != 401:
        fail("a deactivated service account's agent still acts for alice")
    ok("deactivating the service account ended its tokens, and its acting for alice")


def mcp(token: str) -> None:
    """An agent's view: the MCP server, through the ingress, with the app's token."""

    def rpc(
        method: str, params: dict[str, Any], auth: str | None = token, **headers: str
    ) -> requests.Response:
        return requests.post(
            f"{IKI}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25",
                **({"Authorization": f"Bearer {auth}"} if auth else {}),
                **headers,
            },
            allow_redirects=False,
            timeout=30,
        )

    unsigned = rpc("tools/list", {}, auth=None)
    if unsigned.status_code != 401 or "resource_metadata=" not in unsigned.headers.get(
        "WWW-Authenticate", ""
    ):
        fail(f"MCP without a token answered HTTP {unsigned.status_code}: {unsigned.headers}")
    hello = rpc(
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "e2e-agent", "version": "1"},
        },
    )
    if hello.status_code != 200 or hello.json()["result"]["serverInfo"]["name"] != "iki":
        fail(f"MCP initialize answered HTTP {hello.status_code}: {hello.text[:300]}")
    # The ingress passes on the session Iki gives it, and the session it's sent back.
    session = hello.headers.get("Mcp-Session-Id", "")
    if not session:
        fail(f"MCP initialize gave no session: {dict(hello.headers)}")
    if rpc("tools/list", {}, **{"Mcp-Session-Id": session + "x"}).status_code != 404:
        fail("MCP took a session Iki didn't issue")
    called = rpc(
        "tools/call",
        {"name": "list_projects", "arguments": {"member": True}},
        **{"Mcp-Session-Id": session, "Iki-Agent-Model": "e2e-model"},
    )
    result = called.json().get("result", {}) if called.status_code == 200 else {}
    names = {p["name"] for p in result.get("structuredContent", {}).get("items", [])}
    granted = bearer_call(token, "GET", "/projects?member=true").json()["items"]
    if result.get("isError") or not names or names != {p["name"] for p in granted}:
        fail(f"MCP list_projects answered HTTP {called.status_code}: {called.text[:300]}")
    if "<iki-data-" not in result["content"][0]["text"]:
        fail("MCP answers aren't fenced as data")
    # An app's spec, checked by iki-wasm in the web pod: the guide's example, then a typo.
    said = {"Mcp-Session-Id": session}
    guide = rpc("tools/call", {"name": "describe_apps", "arguments": {}}, **said).json()
    example = guide["result"]["structuredContent"]["example"]
    checked = []
    for spec in (example, {**example, "panels": example["panels"][::-1]}):
        answer = rpc("tools/call", {"name": "check_app", "arguments": {"spec": spec}}, **said)
        checked.append(answer.json()["result"]["structuredContent"]["problems"])
    if checked[0] or not any("isn’t a panel before it" in p for p in checked[1]):
        fail(f"MCP check_app found {checked}")
    ok(
        "an agent signed in as alice reached the MCP server in a session of its own and saw "
        f"what the API shows: {names}"
    )


def saml() -> None:
    """Switches sign-in to Keycloak over SAML through recovery, signs in, and switches back."""
    set_up("bob", protocol="saml")
    sign_in_as("alice")
    ok("alice signed in through Keycloak over SAML (signed assertion, SP-initiated)")
    set_up("bob")


def in_a_browser(user: str, ports: tuple[int, int]) -> None:
    """Chromium: sign in through Keycloak's page, then create a project in the web app.

    Runs with the production CSP and Trusted Types; any console error fails it.
    """
    from playwright.sync_api import Error, sync_playwright

    iki_port, keycloak_port = ports
    rules = (
        f"MAP localhost:8000 127.0.0.1:{iki_port}, "
        f"MAP keycloak.iki.localhost:8080 127.0.0.1:{keycloak_port}"
    )
    name = f"Browser {int(time.time())}"
    with sync_playwright() as playwright:
        try:
            chromium = playwright.chromium.launch(args=[f"--host-resolver-rules={rules}"])
        except Error as error:
            fail(f"Chromium didn't start ({error}). Run: uv run playwright install chromium")
        page = chromium.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(f"{IKI}/projects/")
        page.get_by_role("button", name="Continue with Keycloak").click()
        page.locator("#username").fill(user)
        page.locator("#password").fill(secret("keycloak-dev", "IKI_DEV_USER_PASSWORD"))
        page.locator("#kc-login").click()
        page.wait_for_url(f"{IKI}/projects/")
        page.get_by_role("heading", name="Projects", level=1).wait_for()
        ok(f"in Chromium, {user} signed in through Keycloak and came back where they started")

        page.get_by_role("link", name="New project").first.click()
        page.get_by_role("textbox", name="Project name").fill(name)
        page.get_by_role("button", name="Create project").click()
        page.get_by_text(f"Created {name}. You’re its Admin.").wait_for()
        page.get_by_role("link", name="Members").click()
        page.get_by_role("list", name="Members").get_by_text("(you)").wait_for()
        chromium.close()
    if errors:
        fail(f"the web app logged errors: {errors}")
    ok("in Chromium, they created a project in the web app, with no errors or CSP violations")


def check(user: str, admin: requests.Session, ports: tuple[int, int]) -> dict[str, Any]:
    session = browser()
    login = session.get(f"{IKI}/login/")
    if login.status_code != 200 or "Continue with Keycloak" not in login.text:
        fail(f"login page: HTTP {login.status_code}")
    csp = login.headers.get("Content-Security-Policy", "")
    if (
        "default-src 'none'" not in csp
        or "form-action 'self' http://keycloak.iki.localhost:8080" not in csp
    ):
        fail(f"unexpected CSP on the login page: {csp}")
    for header in (
        "Permissions-Policy",
        "Cross-Origin-Opener-Policy",
        "Cross-Origin-Embedder-Policy",
    ):
        if header not in login.headers:
            fail(f"login page lacks {header}")
    ok("login page sends a strict CSP whose form-action allows only Iki and Keycloak")

    action, inputs = form(login)
    response = keycloak_sign_in(
        session, session.post(action, data=inputs, headers={"Origin": IKI}), user
    )
    landed(session, response, user)
    cookie = next((c for c in session.cookies if c.name == "iki_session"), None)
    if cookie is None or not cookie.has_nonstandard_attr("HttpOnly") or cookie.expires is not None:
        fail("session cookie must be HttpOnly and end with the browser session")
    ok(f"{user} signed in through Keycloak with PKCE; session cookie is HttpOnly")

    project = teams_and_projects(admin, session, f"{user}@iki.test")
    identity_provider_mail(user)
    in_a_browser(user, ports)

    action, inputs = form(session.get(f"{IKI}/teams/"))
    response = session.post(action, data=inputs, headers={"Origin": IKI})
    if urlsplit(response.url).path != "/signed-out/":
        fail(f"signing out landed on {response.url}")
    if urlsplit(session.get(f"{IKI}/").url).path != "/login/":
        fail("still signed in after signing out")
    ok("sign-out ends the session")

    logs = kubectl("logs", WEB["target"], "-c", "web", "--since=5m")
    events = [json.loads(line) for line in logs.splitlines() if line.startswith("{")]
    names = {e.get("event") for e in events}
    expected = {
        "setup.completed",
        "user.signed_in",
        "user.signed_out",
        "team.created",
        "project.created",
    }
    if not expected <= names:
        fail(f"expected sign-in events in the web logs, found {sorted(n for n in names if n)}")
    if any("code=" in line or "state=" in line for line in logs.splitlines()):
        fail("OAuth code or state leaked into the web logs")
    ok("audit events reach the log stream for a SIEM; no OAuth codes in the logs")
    return project


def followed(session: requests.Session, run: dict[str, Any], within: int = 120) -> dict[str, Any]:
    """The run, once it has ended, followed as its page does: its live updates through the
    ingress, then its attempts and log."""
    seen: list[str] = []
    found = run
    deadline = time.monotonic() + within
    # Read timeout past the stream's 20-second heartbeat.
    with session.get(
        f"{IKI}/api/v1/runs/{run['id']}/events",
        headers={"Origin": IKI},
        stream=True,
        timeout=(5, 30),
    ) as stream:
        if stream.headers.get("Content-Type", "").split(";")[0] != "text/event-stream":
            fail(f"{run['hrid']}'s live updates: HTTP {stream.status_code} {stream.text[:200]}")
        for raw in stream.iter_lines():
            if time.monotonic() > deadline:
                fail(f"{run['hrid']} didn't finish in {within} seconds: {found['status']}")
            line = raw.decode()
            if not line.startswith("event: "):
                continue
            event = line.removeprefix("event: ")
            seen.append(event)
            if event in ("ready", "run"):
                found = api_call(session, "GET", f"/runs/{run['id']}").json()
                if found["status"] in ("succeeded", "failed", "cancelled"):
                    break
    if seen[:1] != ["ready"]:
        fail(f"{run['hrid']}'s live updates didn't start with ready: {seen}")
    attempts = api_call(session, "GET", f"/runs/{run['id']}/attempts").json()
    last = attempts[-1]
    if not last["worker"] or last["finished_at"] is None:
        fail(f"{run['hrid']}'s attempts don't say which worker ran it: {attempts}")
    log = api_call(session, "GET", f"/runs/{run['id']}/attempts/{last['attempt']}/log").json()
    if not log["ended"] or log["size"] != last["log_size"]:
        fail(f"{run['hrid']}'s log doesn't match its attempt: {log} vs {last}")
    whole = session.get(
        f"{IKI}/api/v1/runs/{run['id']}/attempts/{last['attempt']}/log.txt",
        headers={"Origin": IKI},
        timeout=10,
    )
    if whole.status_code != 200 or whole.text != log["text"]:
        fail(f"{run['hrid']}'s log download answered HTTP {whole.status_code}")
    live = ", ".join(e for e in seen if e != "ready") or "none (it had already ended)"
    ok(
        f"{run['hrid']}'s page followed it live (events: {live}); attempt {last['attempt']} "
        f"ran on {last['worker']} and its {log['size']}-byte log downloads"
    )
    return found


def dna_actions(alice: requests.Session, project: dict[str, Any]) -> None:
    """The DNA plugin's actions, run by the cluster's backend worker in its built environment."""
    listed = {a["name"]: a for a in api_call(alice, "GET", "/actions").json()}
    for name in ("iki/dna.compute_composition", "iki/dna.primer_properties"):
        if not listed.get(name, {}).get("available"):
            fail(f"{name} isn't available to run: {listed.get(name)}")
    # Fetching from NCBI needs the plugin's ncbi_email setting (ADR 0169), which the
    # cluster sets (settings/dna.yaml).
    fetch = listed.get("iki/dna.fetch_sequences", {})
    if not fetch.get("available"):
        fail(f"iki/dna.fetch_sequences isn't available to run: {fetch}")
    # New bases each time, so each check registers new entities.
    bases = "".join(secrets.choice("ACGT") for _ in range(40))
    primer = bases[:20]
    expected = {
        "iki/dna.compute_composition": ("iki/dna.DNASequence", "sequence", bases, {"length": 40}),
        "iki/dna.primer_properties": ("iki/dna.Primer", "primer", primer, {"length": 20}),
    }
    for action, (kind, name, sequence, values) in expected.items():
        made = api_call(
            alice,
            "POST",
            "/entities",
            {"type": kind, "project": project["id"], "values": {"bases": sequence}},
        )
        if made.status_code not in (200, 201):
            fail(f"registering a {kind} answered HTTP {made.status_code}: {made.text[:300]}")
        entity = made.json()
        if not entity.get("registered") or not entity.get("entry"):
            fail(f"a new {kind} wasn't registered: {entity}")
        launched = api_call(
            alice,
            "POST",
            f"/actions/{action}/runs",
            {"project": project["id"], "inputs": {name: entity["id"]}},
        )
        if launched.status_code != 201:
            fail(f"launching {action} answered HTTP {launched.status_code}: {launched.text[:300]}")
        run = followed(alice, launched.json())
        if run["status"] != "succeeded":
            fail(f"{run['hrid']} ({action}) {run['status']}: {run['error']}")
        got = api_call(alice, "GET", f"/entities/{entity['id']}").json()["values"]
        gc = round(100 * sum(b in "GC" for b in sequence) / len(sequence), 2)
        if got.get("length") != values["length"] or got.get("gc_content") != gc:
            fail(f"{run['hrid']} wrote {got}, not length {values['length']} and GC {gc}%")
        if action == "iki/dna.primer_properties" and not isinstance(
            got.get("melting_temperature"), (int, float)
        ):
            fail(f"{run['hrid']} wrote no melting temperature: {got}")
        ok(f"{action} ran on the cluster's backend worker ({run['hrid']}) and wrote {got}")


def strain_stocks(
    alice: requests.Session, project: dict[str, Any], plasmid: dict[str, Any]
) -> None:
    """A catalogue strain, a local transformant and the data from a scanned stock draw."""
    catalogue = Path(__file__).resolve().parents[2] / "plugins/strains/tests/data/atcc.csv"
    file_id = upload(alice, project, "atcc.csv", catalogue.read_bytes(), "text/csv")
    imported = launch(alice, project, "iki/strains.import_base_strains", {}, {"file": file_id})
    entries = [
        record(alice, effect["entity"])
        for effect in imported["effects"]
        if effect["type"] == "iki/strains.BaseStrain"
    ]
    strains = {row["values"]["accession"]: row for row in entries}
    if {key: row["values"]["name"] for key, row in strains.items()} != {
        "700926": "MG1655",
        "201388": "BY4741",
    }:
        fail(f"{imported['hrid']} did not register the two ATCC catalogue strains")
    # The local transformant, barcode, quantities and sequence read are synthetic test data.
    base = strains["700926"]
    identity = {"base": entry_of(base), "plasmids": [entry_of(plasmid)]}
    strain = created(alice, project, "iki/strains.TransformedStrain", identity)
    again = created(alice, project, "iki/strains.TransformedStrain", identity)
    if not again["reused"] or again["id"] != strain["id"]:
        fail(f"Registering {strain['hrid']}'s base and plasmid again created another strain")
    transformation = created(
        alice, project, "iki/experiments.Experiment", {"title": "Synthetic transformation"}
    )
    isolate = created(
        alice,
        project,
        "iki/lots.StrainIsolate",
        {
            "strain": entry_of(strain),
            "isolate_label": "Synthetic clone A",
            "transformation": transformation["id"],
        },
    )
    barcode = f"SYNTHETIC-{secrets.token_hex(4)}"
    stock = created(
        alice,
        project,
        "iki/lots.GlycerolStock",
        {"isolate": isolate["id"], "contents": isolate["id"], "barcode": barcode},
        {"value": 100, "unit": "uL"},
    )
    box = created(alice, project, "iki/inventory.Box", {"name": "Synthetic glycerol stocks"})
    moved(alice, [(stock, box, "A1")], "Synthetic inventory example")
    location = api_call(alice, "GET", f"/entities/{stock['id']}/location").json()
    if [(step["hrid"], step["position"]) for step in location["path"]] != [(box["hrid"], "A1")]:
        fail(f"{stock['hrid']} has an unexpected storage path {location['path']}")
    read = created(
        alice,
        project,
        "iki/dna.DNASequence",
        {
            "name": "Synthetic sequence read",
            "bases": "".join(secrets.choice("ACGT") for _ in range(32)),
        },
    )
    give_lab_roles(alice, project, "iki/lots.qa")
    drawn = api_call(
        alice,
        "POST",
        f"/entities/{stock['id']}/amount/draw",
        {
            "amount": {"value": 10, "unit": "uL"},
            "used_in": read["id"],
            "reason": "Synthetic isolate check",
            "purpose": "qa_sampling",
            "confirm": barcode,
        },
    )
    if drawn.status_code != 200:
        fail(f"Drawing from {stock['hrid']} answered HTTP {drawn.status_code}: {drawn.text[:300]}")
    movement = drawn.json()["movement"]
    if (
        Decimal(drawn.json()["amount"]["value"]) != 90
        or movement["used_in"] != read["id"]
        or movement["confirmed"] != barcode
    ):
        fail(f"{stock['hrid']}'s ledger recorded an unexpected draw {drawn.json()}")
    ancestry = api_call(alice, "GET", f"/entities/{read['id']}/graph?up=5&down=0")
    if ancestry.status_code != 200:
        fail(f"Graph for {read['hrid']} gave HTTP {ancestry.status_code}: {ancestry.text[:500]}")
    graph = ancestry.json()
    ancestors = {entity["id"] for entity in graph["entities"]}
    if (
        not {
            stock["id"],
            isolate["id"],
            transformation["id"],
            strain["id"],
            base["id"],
            plasmid["id"],
        }
        <= ancestors
    ):
        fail(f"{read['hrid']}'s lineage is missing its stock, isolate or strain ancestry")
    ok(f"{read['hrid']} traces to {strain['hrid']} through the draw from {stock['hrid']} at A1")


def _abif(bases: str, quality: list[int], sample: str) -> bytes:
    """A minimal ABIF (AB1) trace file, as a sequencer writes: its called bases (PBAS 2),
    their qualities (PCON 2) and the sample's name (SMPL 1)."""
    entries = [
        (b"PBAS", 2, 2, bases.encode()),
        (b"PCON", 2, 2, bytes(quality)),
        (b"SMPL", 1, 18, bytes([len(sample)]) + sample.encode()),
    ]
    data, directory, start = b"", b"", 128
    for name, number, kind, payload in entries:
        size = len(payload)
        directory += struct.pack(
            ">4sihhiiii", name, number, kind, 1, size, size, start + len(data), 0
        )
        data += payload
    header = struct.pack(
        ">4sh4sihhiiii",
        b"ABIF",
        101,
        b"tdir",
        1,
        1023,
        28,
        len(entries),
        len(directory),
        start + len(data),
        0,
    )
    return header.ljust(start, b"\0") + data + directory


def reads_aligned(alice: requests.Session, project: dict[str, Any]) -> None:
    """iki/dna.align_reads: a forward trace with a difference, and a reverse read across the
    origin, uploaded as a list of files, aligned to a circular plasmid they come from."""
    plasmid = "".join(secrets.choice("ACGT") for _ in range(1200))
    made = api_call(
        alice,
        "POST",
        "/entities",
        {
            "type": "iki/dna.DNASequence",
            "project": project["id"],
            "values": {"name": "pE2E", "bases": plasmid, "topology": "circular"},
        },
    )
    if made.status_code not in (200, 201):
        fail(f"registering a plasmid answered HTTP {made.status_code}: {made.text[:300]}")
    reference = made.json()
    design = created(
        alice,
        project,
        "iki/plasmid.Plasmid",
        {"sequence": entry_of(reference), "name": "Synthetic read-verification control"},
    )
    prep = created(
        alice,
        project,
        "iki/lots.PlasmidPrep",
        {"plasmid": design["id"], "notes": "Synthetic prep for the read-verification control."},
    )
    # Bases 201-700, read forward, with another base at 451, between poor ends.
    called = list(plasmid[200:700])
    other = "A" if called[250] != "A" else "C"
    called[250] = other
    trace = _abif("TTTT" + "".join(called) + "GGGG", [4] * 4 + [40] * 500 + [4] * 4, "F1")
    # Bases 1101-1200 and 1-200, read on the other strand.
    across = plasmid[1100:] + plasmid[:200]
    backward = across.translate(str.maketrans("ACGT", "TGCA"))[::-1]
    fastq = f"@R1\n{backward}\n+\n{'I' * len(backward)}\n".encode()
    files = [
        upload(alice, project, "clone_F1.ab1", trace, "application/x-abi"),
        upload(alice, project, "clone_R1.fastq", fastq, "text/x-fastq"),
    ]
    action = "iki/dna.align_reads"
    run = launch(alice, project, action, {"reference": reference["id"]}, {"reads": files})
    metrics = run.get("metrics") or {}
    if metrics.get("aligned") != 2 or metrics.get("differences") != 1:
        fail(f"{run['hrid']} ({action}) measured {metrics}, not 2 reads and 1 difference")
    said = [m["text"] for m in run.get("messages") or []]
    # An AB1 trace's read is named by its file.
    expected = f"Differences from {reference['hrid']}: o.451{plasmid[450]}>{other} (clone_F1 Q40)."
    if expected not in said:
        fail(f"{run['hrid']} said {said}, not {expected!r}")
    alignments = [e for e in run.get("effects") or [] if e["kind"] == "created"]
    if [e["type"] for e in alignments] != ["iki/core.Alignment"]:
        fail(f"{run['hrid']} created {alignments}, not one iki/core.Alignment")
    values = api_call(alice, "GET", f"/entities/{alignments[0]['hrid']}").json()["values"]
    plasmid_entry = entry_of(reference)
    if values["reference"] != plasmid_entry or values["members"] != [plasmid_entry]:
        fail(f"{alignments[0]['hrid']} is {values}")
    # It keeps Iki's alignment, its file, and the same with `-` for every gap, which tools
    # reading aligned FASTA take (alignment-standard.fasta).
    artifacts = api_call(alice, "GET", f"/runs/{run['hrid']}/artifacts").json()
    kept = {a["name"]: a for a in artifacts}
    if set(kept) != {"alignment.fasta", "alignment-standard.fasta"}:
        fail(f"{run['hrid']} kept {sorted(kept)}, not its alignment and the standard one")
    stored = api_call(alice, "GET", f"/files/{values['aligned']}/content-url").json()
    if stored["sha256"] != kept["alignment.fasta"]["sha256"]:
        fail(f"{alignments[0]['hrid']}'s file isn't the alignment.fasta {run['hrid']} kept")
    ok(
        f"{action} aligned a trace and a read across the origin of a plasmid ({run['hrid']}): "
        f"{metrics['coverage_percent']}% covered, {expected[:-1].split(': ', 1)[1]}, "
        f"making {alignments[0]['hrid']}"
    )
    verify_prep(alice, project, prep, design, record(alice, alignments[0]["entity"]))


def verify_prep(
    alice: requests.Session,
    project: dict[str, Any],
    prep: dict[str, Any],
    design: dict[str, Any],
    alignment: dict[str, Any],
) -> None:
    """Archive the synthetic prep's alignment, sign its discrepancy and open it from the prep."""
    run = launch(
        alice,
        project,
        "iki/lots.record_plasmid_verification",
        {"prep": prep["id"], "alignment": alignment["id"]},
        {"outcome": "discrepancy", "notes": "Synthetic control: partial coverage, one mismatch."},
    )
    (effect,) = [e for e in run["effects"] if e["type"] == "iki/lots.PlasmidVerification"]
    assessment = record(alice, effect["entity"])
    values = assessment["values"]
    expected = {
        "prep": prep["id"],
        "plasmid": entry_of(design),
        "plasmid_record_id": design["id"],
        "alignment": alignment["id"],
        "alignment_revision": alignment["revision"],
        "review_flow": "research",
        "review": "recorded",
        "outcome": "discrepancy",
    }
    if any(values.get(k) != v for k, v in expected.items()):
        fail(f"{run['hrid']} created {assessment['hrid']} with {values}, expected {expected}")
    pinned = api_call(
        alice,
        "GET",
        f"/entities/{values['plasmid_record_id']}/revisions/{values['plasmid_revision']}",
    )
    evidence = api_call(
        alice, "GET", f"/entities/{values['alignment']}/revisions/{values['alignment_revision']}"
    )
    if (
        pinned.status_code != 200
        or evidence.status_code != 200
        or pinned.json()["values"]["sequence"] != alignment["values"]["reference"]
        or evidence.json()["values"]["aligned"] != alignment["values"]["aligned"]
    ):
        fail(f"{assessment['hrid']} could not reopen the recorded plasmid and alignment revisions")
    give_lab_roles(alice, project, "iki/lots.verification_reviewer")
    reviewer = api_call(alice, "GET", "/me").json()["id"]
    path = f"/entities/{assessment['id']}"
    assigned = api_call(
        alice,
        "PATCH",
        path,
        {"values": {"reviewer": reviewer}},
        {"If-Match": api_call(alice, "GET", path).headers["ETag"]},
    )
    if assigned.status_code != 200:
        fail(f"assigning the reviewer answered {assigned.status_code}: {assigned.text[:300]}")
    signed = signing(alice, assessment, "signed_off", "alice", field="review")
    if signed["status"] != "signed" or not signed["set"]:
        fail(f"signing the research assessment {assessment['hrid']} gave {signed}")
    tab = api_call(
        alice, "POST", "/components/iki/lots.prep_verifications/entities", {"entity": prep["id"]}
    )
    if tab.status_code != 200 or [r["hrid"] for r in tab.json()["items"]] != [assessment["hrid"]]:
        fail(f"{prep['hrid']}'s Verification tab did not return its assessment: {tab.text[:300]}")
    row = tab.json()["items"][0]["values"]
    reviewed = {
        "plasmid_record_id": design["id"],
        "plasmid_revision": values["plasmid_revision"],
        "alignment": alignment["id"],
        "alignment_revision": alignment["revision"],
        "reviewer": reviewer,
        "outcome": "discrepancy",
        "review": "signed_off",
    }
    if any(row.get(k) != v for k, v in reviewed.items()):
        fail(f"{prep['hrid']}'s Verification tab returned {row}")
    ok(f"{prep['hrid']} shows the signed discrepancy and the pinned alignment {alignment['hrid']}")


def sequences_aligned(alice: requests.Session, project: dict[str, Any]) -> None:
    """iki/dna.align_pair, align_multiple and consensus: a clone of a plasmid, one base
    changed and registered from its other strand, aligned to the plasmid as designed, as
    needle aligns two and through MAFFT in a pod of its own; then the MAFFT alignment's
    consensus, the design left out, which is the clone, found in the registry."""
    # pUC19 with a new tag in its polylinker each time, so each check registers new ones.
    tag = "".join(secrets.choice("ACGT") for _ in range(24))
    designed_bases = PUC19[:420] + tag + PUC19[420:]
    clone_bases = designed_bases[:1500] + ("A" if designed_bases[1500] != "A" else "C")
    clone_bases += designed_bases[1501:]
    other_strand = clone_bases.translate(str.maketrans("ACGT", "TGCA"))[::-1]
    made = []
    for name, bases in (("pUC19-tag", designed_bases), ("pUC19-tag clone 3", other_strand)):
        got = api_call(
            alice,
            "POST",
            "/entities",
            {
                "type": "iki/dna.DNASequence",
                "project": project["id"],
                "values": {"name": name, "bases": bases, "topology": "circular"},
            },
        )
        if got.status_code not in (200, 201):
            fail(f"registering {name} answered HTTP {got.status_code}: {got.text[:300]}")
        made.append(got.json())
    designed, clone = made
    length = len(designed_bases)

    action = "iki/dna.align_pair"
    run = launch(alice, project, action, {"query": clone["id"], "target": designed["id"]})
    metrics = run.get("metrics") or {}
    one_off = 100 * (length - 1) / length
    if metrics.get("length") != length or abs(metrics.get("identity_percent", 0) - one_off) > 1e-9:
        fail(f"{run['hrid']} ({action}) measured {metrics}, not {length} columns, one differing")
    created = [e for e in run.get("effects") or [] if e["kind"] == "created"]
    values = record(alice, created[0]["entity"])["values"]
    if not values["method"].endswith("the query's reverse complement"):
        fail(f"{run['hrid']} didn't align {clone['hrid']} as its other strand: {values}")
    ok(
        f"{action} aligned {clone['hrid']} as its other strand to {designed['hrid']} "
        f"({run['hrid']}): {metrics['identity_percent']:.2f}% identical, making "
        f"{created[0]['hrid']}"
    )

    action = "iki/dna.align_multiple"
    run = launch(
        alice,
        project,
        action,
        {"sequences": [clone["id"]], "reference": designed["id"]},
        within=900,
    )
    (attempt,) = api_call(alice, "GET", f"/runs/{run['id']}/attempts").json()
    if attempt["environment_build"] != DNA_ALIGNMENT_TOOLS.rpartition("@")[2]:
        fail(f"{run['hrid']} didn't run in the dna plugin's alignment-tools image: {attempt}")
    if attempt["template"] != "container-alignments":
        fail(f"{run['hrid']} didn't run on the alignments' template: {attempt}")
    metrics = run.get("metrics") or {}
    if metrics.get("rows") != 2 or metrics.get("reversed") != 1:
        fail(f"{run['hrid']} ({action}) measured {metrics}, not two rows, one turned round")
    created = [e for e in run.get("effects") or [] if e["kind"] == "created"]
    if [e["type"] for e in created] != ["iki/core.Alignment"]:
        fail(f"{run['hrid']} created {created}, not one iki/core.Alignment")
    alignment = created[0]
    values = record(alice, alignment["entity"])["values"]
    members = [entry_of(designed), entry_of(clone)]
    if values["members"] != members or values["reference"] != members[0]:
        fail(f"{alignment['hrid']} is {values}")
    ok(
        f"{action} ran MAFFT in a pod of its own ({run['hrid']}): {values['method']}, "
        f"{clone['hrid']} turned round, making {alignment['hrid']}"
    )

    action = "iki/dna.consensus"
    run = launch(
        alice,
        project,
        action,
        {"alignment": alignment["entity"]},
        {"include_reference": False, "topology": "circular"},
    )
    found = [e for e in run.get("effects") or [] if e["type"] == "iki/dna.DNASequence"]
    if [(e["kind"], e["entity"]) for e in found] != [("reused", clone["id"])]:
        fail(f"{run['hrid']} ({action}) made {run['effects']}, not {clone['hrid']} found")
    ok(
        f"{action}: {alignment['hrid']}'s consensus, the design left out, is {clone['hrid']}, "
        f"found in the registry ({run['hrid']})"
    )


#: The fake LIMS's records (deploy/dev/fixtures/fake-lims.yaml).
LIMS_RECORDS = ("LIMS-0001", "LIMS-0002", "LIMS-0003")


def lims(alice: requests.Session, project: dict[str, Any]) -> None:
    """Run the example connector against the fake LIMS, importing records over HTTPS,
    through the egress proxy, at the host the deployment's settings give, then again,
    matching each record to its protein, and a write-back."""
    action = "acme/lims_connector.import_from_lims"
    run = launch(alice, project, action, {})
    if run["network"] != ["fake-lims.iki.svc"]:
        fail(f"{run['hrid']} may reach {run['network']}, not the host lims_host gives")
    attempt = api_call(alice, "GET", f"/runs/{run['id']}/attempts").json()[-1]
    reached = [(e["host"], e["port"], e["refused"]) for e in attempt["network"]]
    if reached != [("fake-lims.iki.svc", 443, None)]:
        fail(f"{run['hrid']} reached {reached}, not the fake LIMS alone")
    if sorted(e["type"] for e in run["effects"]) != ["iki/protein.Protein"] * 3:
        fail(f"{run['hrid']} wrote {run['effects']}, not the LIMS's three proteins")
    found = {}
    for record in LIMS_RECORDS:
        query = f"type=iki/protein.Protein&field=lims_id&value={record}&project={project['id']}"
        matches = api_call(alice, "GET", f"/entities/lookup?{query}").json()
        if len(matches) != 1:
            fail(f"looking {record} up found {matches}, not one protein")
        found[record] = matches[0]
    ok(
        f"{action} reached the fake LIMS over HTTPS through the egress proxy ({run['hrid']}): "
        + ", ".join(f"{r} is {e['hrid']}" for r, e in found.items())
    )
    again = launch(alice, project, action, {})
    kinds = sorted(e["kind"] for e in again["effects"])
    if kinds != ["reused"] * 3 or {e["entity"] for e in again["effects"]} != {
        e["id"] for e in found.values()
    }:
        fail(f"importing again ({again['hrid']}) gave {again['effects']}, not the same three")
    ok(f"importing again matched each record to its protein, unchanged ({again['hrid']})")
    first = found[LIMS_RECORDS[0]]
    sent = launch(alice, project, "acme/lims_connector.send_to_lims", {"proteins": [first["id"]]})
    deadline = time.monotonic() + 30
    while True:
        patched = [
            r
            for r in requests.get(f"{LIMS['url']}/requests", timeout=5).json()
            if r["method"] == "PATCH" and r["path"] == f"/api/v1/proteins/{LIMS_RECORDS[0]}"
        ]
        if patched and patched[-1]["body"].get("iki_id") == first["hrid"]:
            break
        if time.monotonic() > deadline:
            fail(f"{sent['hrid']} didn't write {first['hrid']} to {LIMS_RECORDS[0]}: {patched}")
        time.sleep(1)
    kept = api_call(alice, "GET", f"/runs/{sent['hrid']}/artifacts").json()
    if [a["path"] for a in kept] != ["sent.json"]:
        fail(f"{sent['hrid']} kept {kept}, not what it sent")
    ok(f"{sent['hrid']} wrote {first['hrid']} to external record {LIMS_RECORDS[0]}")


#: acme/liquid_handling's Tecan worklist (ADR 0230) for one transfer: an aspirate from
#: well B3 of a 384-well source plate (column 3, row B: (3 - 1) x 16 + 2 = 34), a dispense
#: to A1 of a 96-well plate (position 1), then a wash, as Freedom EVOware reads it.
TECAN_WORKLIST = (
    b"A;Compound source 1;CP-0412;384 Well Greiner PP;34;;2.5;DMSO Free Single;;;\r\n"
    b"D;Assay plate 1;AP-0031;96 Well Corning Flat;1;;2.5;DMSO Free Single;;;\r\n"
    b"W;\r\n"
)


def plugin_export(alice: requests.Session, project: dict[str, Any]) -> None:
    """Download a plugin's format: a liquid handler's worklist for a plate-to-plate
    transfer, written by acme/liquid_handling's Wasm writer in the export sandbox,
    and audited with the module that wrote it."""
    kind = "acme/liquid_handling."
    source = created(
        alice,
        project,
        kind + "Plate",
        {
            "label": "Compound source 1",
            "barcode": "CP-0412",
            "labware_type": "384 Well Greiner PP",
            "wells": "plate_384",
        },
    )
    destination = created(
        alice,
        project,
        kind + "Plate",
        {
            "label": "Assay plate 1",
            "barcode": "AP-0031",
            "labware_type": "96 Well Corning Flat",
            "wells": "plate_96",
        },
    )
    transfer = created(
        alice,
        project,
        kind + "Transfer",
        {
            "source": entry_of(source),
            "source_well": "B3",
            "destination": entry_of(destination),
            "destination_well": "A1",
            "volume": {"value": 2.5, "unit": "uL"},
            "liquid_class": "DMSO Free Single",
        },
    )
    format_name = kind + "tecan-worklist"
    query = urlencode({"format": format_name})
    got = api_call(alice, "GET", f"/entities/{transfer['hrid']}/export?{query}")
    if got.status_code != 200:
        fail(f"exporting {transfer['hrid']} answered HTTP {got.status_code}: {got.text[:300]}")
    media_type = got.headers.get("Content-Type", "")
    if not media_type.startswith("text/x-tecan-gwl"):
        fail(f"{transfer['hrid']}'s worklist came as {media_type!r}, not text/x-tecan-gwl")
    if got.content != TECAN_WORKLIST:
        fail(f"{transfer['hrid']}'s worklist is {got.content!r}, not {TECAN_WORKLIST!r}")
    digest = hashlib.sha256(got.content).hexdigest()
    audited = json.loads(
        kubectl(
            "exec",
            "-i",
            WEB["target"],
            "-c",
            "web",
            "--",
            "iki-server",
            "shell",
            stdin=(
                "import json\n"
                "from iki_server.audit.models import AuditEvent\n"
                "event = AuditEvent.objects.filter(\n"
                f"    action='entity.exported', target_id={str(transfer['id'])!r}\n"
                ").order_by('-occurred_at').first()\n"
                "print(json.dumps(event.data if event else None))\n"
            ),
        )
        .strip()
        .splitlines()[-1]
    )
    writer = (audited or {}).get("writer") or {}
    if (
        not audited
        or audited.get("format") != format_name
        or audited.get("sha256") != digest
        or writer.get("plugin") != "acme/liquid_handling"
        or not re.fullmatch(r"[0-9a-f]{64}", str(writer.get("module_sha256", "")))
    ):
        fail(f"exporting {transfer['hrid']} was audited as {audited}")
    ok(
        f"{transfer['hrid']} downloads as a Tecan worklist that acme/liquid_handling's writer "
        f"wrote in the export sandbox (SHA-256 {digest[:12]}…), audited with module "
        f"{writer['module_sha256'][:12]}…"
    )


def sequencing(alice: requests.Session, project: dict[str, Any]) -> None:
    """Trace paired reads from a public yeast library to a signed quality summary."""
    prefix = "iki/sequencing."
    give_lab_roles(alice, project, prefix + "reviewer")
    accession = "SRR8690267"
    runs = matching_records(alice, project, prefix + "SequencingRun", "run_accession", accession)
    run = (
        runs[0]
        if runs
        else created(
            alice,
            project,
            prefix + "SequencingRun",
            {
                "name": "Yeast RNA-seq excerpt",
                "instrument_model": "Illumina HiSeq 2500",
                "run_accession": accession,
            },
        )
    )
    read_sets = matching_records(alice, project, prefix + "ReadSet", "run", run["id"])
    if read_sets:
        reads = read_sets[0]
    else:
        sample = created(
            alice,
            project,
            "iki/inventory.Tube",
            {"name": "Saccharomyces cerevisiae SAMN11070697"},
        )
        library = created(
            alice,
            project,
            prefix + "SequencingLibrary",
            {
                "name": "SAMN11070697 RNA-Seq",
                "sample": sample["id"],
                "sample_accession": "SAMN11070697",
                "strategy": "RNA-Seq",
            },
        )
        directory = Path(__file__).resolve().parents[2] / "plugins/sequencing/tests/data"
        files = {}
        for mate in (1, 2):
            fixture = directory / f"{accession}_{mate}.fastq"
            files[f"read_{mate}"] = upload(
                alice, project, fixture.name, fixture.read_bytes(), "text/plain"
            )
        reads = created(
            alice,
            project,
            prefix + "ReadSet",
            {"run": run["id"], "library": library["id"], **files},
        )
    summaries = matching_records(alice, project, prefix + "FASTQSummary", "read_set", reads["id"])
    if summaries:
        summary = record(alice, summaries[0]["id"])
    else:
        analysis = launch(alice, project, prefix + "summarize_fastq", {"reads": reads["id"]})
        effect = next(e for e in analysis["effects"] if e["type"] == prefix + "FASTQSummary")
        summary = record(alice, effect["entity"])
    counts = [(row["reads"], row["q30_percent"]) for row in summary["values"]["statistics"]]
    if counts != [(4, 90.0), (4, 14.0)]:
        fail(f"{summary['hrid']} recorded unexpected FASTQ excerpt statistics {counts}")
    if summary["values"]["review"] == "recorded":
        signature = signing(alice, summary, "signed_off", "alice", field="review")
        if signature["status"] != "signed" or not signature["set"]:
            fail(f"signing {summary['hrid']} gave {signature}")
    ok(f"{reads['hrid']}: signed quality summary for four public yeast read pairs")


def sequencing_analysis(alice: requests.Session, project: dict[str, Any]) -> None:
    """Import raw counts from one sample of a public yeast count matrix."""
    prefix = "iki/sequencing_analysis."
    library_type = "iki/sequencing.SequencingLibrary"
    name = "GEO GSE176296 WTJ.1 excerpt"
    libraries = matching_records(alice, project, library_type, "name", name)
    if libraries:
        library = libraries[0]
    else:
        sample = created(alice, project, "iki/inventory.Tube", {"name": name})
        library = created(
            alice,
            project,
            library_type,
            {"name": name, "sample": sample["id"], "strategy": "RNA-Seq"},
        )
    seen = matching_records(
        alice, project, prefix + "SequencingAnalysisResult", "library", library["id"]
    )
    if seen:
        result = record(alice, seen[0]["id"])
    else:
        fixture = (
            Path(__file__).resolve().parents[2]
            / "plugins/sequencing_analysis/tests/data/GSE176296-counts.tsv"
        )
        report = upload(
            alice, project, fixture.name, fixture.read_bytes(), "text/tab-separated-values"
        )
        imported = launch(
            alice,
            project,
            prefix + "import_result",
            {"library": library["id"]},
            {
                "format": "counts",
                "report": report,
                "tool": "featureCounts",
                "tool_version": "Reported in GEO GSE176296",
                "reference": "sacCer3",
                "column": "WTJ.1",
            },
        )
        artifacts = api_call(alice, "GET", f"/runs/{imported['hrid']}/artifacts")
        if [item["path"] for item in artifacts.json()] != ["selected.tsv"]:
            fail(f"{imported['hrid']} did not retain the selected sample rows")
        effect = next(
            e for e in imported["effects"] if e["type"] == prefix + "SequencingAnalysisResult"
        )
        result = record(alice, effect["entity"])
    summary = result["values"]["summary"]
    if summary["selector"] != "WTJ.1" or summary["metrics"] != {
        "Genes": 4,
        "Total raw count": 1097,
    }:
        fail(f"{result['hrid']} did not retain the selected sample and four gene counts")
    ok(f"{result['hrid']}: four public GEO gene counts linked to one library")


#: What the long-read tools' minimap2 2.31 makes of 41 reads of SRA run SRR17915342 (a
#: MinION run of pUC19) against L09137.2, each read counted once: its plasmid's multiple
#: cloning site differs, and two single-base deletions in runs of six and seven bases are
#: candidate nanopore errors for review. (A third such base, 2043, has no majority of reads
#: and is left uncalled.)
WHOLE_PLASMID_DIFFERENCES = [
    "o.413_418del",
    "o.419_420insACTGGCGGATTGTCAGGAACGCGACCCGGCGCTGTCCGAACTGTCCGAAATTCATGAGGCCGTTCTGACGCGCTGGCCATTGGT",
    "o.423T>A",
    "o.887del",
    "o.1446del",
]


def whole_plasmid(alice: requests.Session, project: dict[str, Any]) -> None:
    """Public pUC19 nanopore reads checked against the plasmid's registered sequence in the
    conda environment workers build for either platform (ADR 0208), the result linked as a
    prep's verification evidence (ADR 0184), and the reads' consensus compared again as a
    service's would be."""
    prefix = "iki/plasmid_sequencing."
    plasmid = next(
        (
            found
            for found in matching_records(alice, project, "iki/plasmid.Plasmid", "name", "pUC19")
            if found["values"].get("annotated_features")
        ),
        None,
    )
    if plasmid is None:
        _, plasmid = register_puc19(alice, project)
        launch(alice, project, "iki/plasmid.annotate_plasmid", {"plasmid": plasmid["id"]})
    accession = "SRR17915342"
    runs = matching_records(
        alice, project, "iki/sequencing.SequencingRun", "run_accession", accession
    )
    run = (
        runs[0]
        if runs
        else created(
            alice,
            project,
            "iki/sequencing.SequencingRun",
            {"name": "pUC19 MinION run", "instrument_model": "MinION", "run_accession": accession},
        )
    )
    read_sets = matching_records(alice, project, "iki/sequencing.ReadSet", "run", run["id"])
    if read_sets:
        reads = read_sets[0]
        library = record(alice, reads["values"]["library"])
        prep = record(alice, library["values"]["sample"])
    else:
        prep = created(
            alice,
            project,
            "iki/lots.PlasmidPrep",
            {"plasmid": entry_of(plasmid), "notes": "pUC19 from BioSample SAMN25333912."},
        )
        library = created(
            alice,
            project,
            "iki/sequencing.SequencingLibrary",
            {
                "name": "SAMN25333912 whole plasmid",
                "sample": prep["id"],
                "sample_accession": "SAMN25333912",
                "strategy": "WGS",
            },
        )
        fixture = (
            Path(__file__).resolve().parents[2]
            / "plugins/plasmid_sequencing/tests/data/SRR17915342-every-900th.fastq.gz"
        )
        read_1 = upload(alice, project, fixture.name, fixture.read_bytes(), "application/gzip")
        reads = created(
            alice,
            project,
            "iki/sequencing.ReadSet",
            {"run": run["id"], "library": library["id"], "read_1": read_1},
        )
    results = matching_records(
        alice, project, prefix + "WholePlasmidAlignment", "read_set", reads["id"]
    )
    if results:
        result = record(alice, results[0]["id"])
    else:
        checked = launch(
            alice,
            project,
            prefix + "verify_plasmid_reads",
            {"plasmid": plasmid["id"], "reads": reads["id"]},
            within=600,
        )
        effect = next(
            e for e in checked["effects"] if e["type"] == prefix + "WholePlasmidAlignment"
        )
        result = record(alice, effect["entity"])
        kept = sorted(
            a["path"] for a in api_call(alice, "GET", f"/runs/{checked['hrid']}/artifacts").json()
        )
        if kept != ["alignments.paf.gz", "consensus.fasta", "coverage.tsv", "differences.tsv"]:
            fail(f"{checked['hrid']} kept {kept}")
    values = result["values"]
    if values["consensus_caller"] != "iki_majority_1" or values["polishing_rounds"] is None:
        fail(f"{result['hrid']} doesn't name the caller that called its consensus")
    named = [d["hgvs"] for d in values["differences"]]
    if (
        values["verdict"],
        named,
        values["reads_aligned"],
        values["reference"],
    ) != ("differs", WHOLE_PLASMID_DIFFERENCES, 37, plasmid["values"]["sequence"]):
        fail(
            f"{result['hrid']} recorded {values['verdict']} {named} "
            f"from {values['reads_aligned']} reads"
        )
    # Each read counts once, however many times it passes a base.
    depths = [*values["coverage"]["mean"], *(d["depth"] for d in values["differences"])]
    deepest = max(depths, default=0)
    if deepest > values["reads_aligned"] or values["unplaced"]:
        fail(
            f"{result['hrid']} counted {deepest} reads where {values['reads_aligned']} "
            f"aligned, or left {values['unplaced']} of its consensus unplaced"
        )
    ok(
        f"{result['hrid']}: 37 of 41 public pUC19 reads, mean depth {values['mean_depth']}, "
        f"{len(named)} differences from L09137.2 named and placed"
    )
    verifications = matching_records(
        alice, project, "iki/lots.PlasmidVerification", "alignment", result["id"]
    )
    if not verifications:
        launched = launch(
            alice,
            project,
            "iki/lots.record_plasmid_verification",
            {"prep": prep["id"], "alignment": result["id"]},
            {
                "outcome": "discrepancy",
                "notes": "Its multiple cloning site differs from L09137.2, every read agreeing.",
            },
        )
        effect = next(e for e in launched["effects"] if e["type"] == "iki/lots.PlasmidVerification")
        verifications = [record(alice, effect["entity"])]
    assessment = verifications[0]["values"]
    if (assessment["alignment"], assessment["prep"]) != (result["id"], prep["id"]):
        fail(f"{verifications[0]['hrid']} links {assessment}, not {result['hrid']}")
    # The reads' consensus, sent back as a service's would be, says the same of the plasmid.
    again = [
        found
        for found in matching_records(
            alice, project, prefix + "WholePlasmidAlignment", "source", "consensus"
        )
        if found["values"].get("consensus_given") == values["consensus"]
    ]
    if again:
        compared = record(alice, again[0]["id"])
    else:
        run_ = launch(
            alice,
            project,
            prefix + "verify_plasmid_consensus",
            {"plasmid": plasmid["id"]},
            {
                "consensus": values["consensus"],
                "provider": "Iki majority consensus 1, from SRR17915342",
            },
        )
        effect = next(e for e in run_["effects"] if e["type"] == prefix + "WholePlasmidAlignment")
        compared = record(alice, effect["entity"])
    if [d["hgvs"] for d in compared["values"]["differences"]] != WHOLE_PLASMID_DIFFERENCES:
        fail(f"{compared['hrid']} found {compared['values']['differences']}")
    if compared["values"]["consensus_caller"] != "supplied":
        fail(f"{compared['hrid']} doesn't say its consensus was supplied")
    ok(
        f"{verifications[0]['hrid']} links {result['hrid']} as {prep['hrid']}'s evidence, and "
        f"its consensus compared alone ({compared['hrid']}) names the same differences"
    )


def proteomics(alice: requests.Session, project: dict[str, Any]) -> None:
    """Import the HUPO-PSI mzTab excerpt for legacy PRIDE 16649."""
    prefix = "iki/proteomics."
    name = "PRIDE 16649 visual cortex mzTab excerpt"
    seen = matching_records(alice, project, prefix + "ProteomicsDataset", "name", name)
    dataset = (
        seen[0]
        if seen
        else created(
            alice,
            project,
            prefix + "ProteomicsDataset",
            {"name": name, "archive": "https://doi.org/10.1074/mcp.M110.005413"},
        )
    )
    results = matching_records(
        alice, project, prefix + "ProteomicsResult", "dataset", dataset["id"]
    )
    if results:
        result = record(alice, results[0]["id"])
    else:
        fixture = (
            Path(__file__).resolve().parents[2] / "plugins/proteomics/tests/data/PRIDE-16649.mztab"
        )
        report = upload(alice, project, fixture.name, fixture.read_bytes(), "text/plain")
        run = launch(
            alice,
            project,
            prefix + "import_search",
            {"dataset": dataset["id"]},
            {"format": "mztab_1_0", "version": "PRIDE 1.0 rc5", "reports": [report]},
        )
        artifact = api_call(alice, "GET", f"/runs/{run['hrid']}/artifacts")
        if [item["path"] for item in artifact.json()] != ["identifications.jsonl"]:
            fail(f"{run['hrid']} did not retain the imported identifications")
        effect = next(e for e in run["effects"] if e["type"] == prefix + "ProteomicsResult")
        result = record(alice, effect["entity"])
    counts = result["values"]["summary"]["counts"]
    if counts != {"protein_group": 2, "spectrum_match": 2}:
        fail(f"{result['hrid']} recorded unexpected mzTab counts {counts}")
    tables = [source["table"] for source in result["values"]["summary"]["sources"]]
    if tables != ["mzTab 1.0 rc5"]:
        fail(f"{result['hrid']} read its report as {tables}, not the PRIDE mzTab 1.0 rc5 export")
    ok(f"{result['hrid']}: imported the PRIDE 16649 proteomics excerpt")


def qc_release(alice: requests.Session, project: dict[str, Any]) -> None:
    """Release a teaching lot against a public USP excerpt and a signed OOS decision."""
    prefix = "iki/qc_release."
    give_lab_roles(alice, project, prefix + "analyst", prefix + "reviewer", prefix + "qa")
    # PubChem CID 5234 identifies sodium chloride. All local lot and test data below are synthetic.
    material = created(
        alice,
        project,
        "iki/small_molecule.Compound",
        {
            "name": "Sodium chloride",
            "smiles": "[Na+].[Cl-]",
            "inchikey": "FAPWRFPIFSIZLT-UHFFFAOYSA-M",
            "pubchem_cid": "5234",
        },
    )
    lot = created(
        alice,
        project,
        "iki/lots.GenericLot",
        {"of": material["id"], "lot_number": "Synthetic NaCl release example", "source": "bought"},
    )

    def effect(run: dict[str, Any], kind: str) -> dict[str, Any]:
        [made] = [e["entity"] for e in run["effects"] if e["type"] == prefix + kind]
        return record(alice, made)

    def sign(entity: dict[str, Any], field: str, state: str, reason: str = "") -> None:
        result = signing(alice, entity, state, "alice", field=field, reason=reason)
        if result["status"] != "signed" or not result["set"]:
            fail(f"signing {entity['hrid']} gave {result}")

    fixture = (
        Path(__file__).resolve().parents[2]
        / "plugins/qc_release/tests/data/sodium-chloride-specification.json"
    )
    specification = json.loads(fixture.read_text(encoding="utf-8"))
    schedule = created(
        alice, project, prefix + "SpecificationSchedule", {"material": material["id"]}
    )
    version = created(
        alice,
        project,
        prefix + "SpecificationVersion",
        {"schedule": schedule["id"], **specification},
    )
    sign(version, "approval", "approved")
    approved = record(alice, version["id"])
    launch(
        alice,
        project,
        prefix + "publish_version",
        {"schedule": schedule["id"], "version": version["id"]},
    )
    case = effect(
        launch(
            alice,
            project,
            prefix + "open_release",
            {"lot": lot["id"], "schedule": schedule["id"]},
            {"testing_on": datetime.now(UTC).date().isoformat()},
        ),
        "ReleaseCase",
    )
    assay, drying = specification["criteria"]

    def test(criterion: dict[str, Any], value: float, **inputs: str) -> dict[str, Any]:
        params = {key: criterion[key] for key in ("attribute", "method", "unit")}
        return launch(
            alice,
            project,
            prefix + "record_test",
            {"case": case["id"], **inputs},
            {**params, "value": value},
        )

    first = test(assay, 98.7)
    original = effect(first, "ReleaseTest")
    investigation = effect(first, "ResearchOOSInvestigation")
    evidence = upload(
        alice,
        project,
        "synthetic-investigation.txt",
        b"Synthetic training evidence. The original record used another sample's report.",
        "text/plain",
    )
    amended = api_call(
        alice,
        "PATCH",
        f"/entities/{investigation['id']}",
        {
            "values": {
                "resolution": "invalidated",
                "findings": "Synthetic scenario: the report ID belongs to a different specimen.",
                "evidence": evidence,
            },
            "reason": "Record the synthetic investigation evidence.",
        },
        {"If-Match": f'"{investigation["revision"]}"'},
    )
    if amended.status_code != 200:
        fail(f"recording an OOS disposition answered HTTP {amended.status_code}: {amended.text}")
    sign(investigation, "review", "signed_off", "Signed synthetic investigation conclusion.")
    repeat = effect(
        test(assay, 99.9, repeat_of=original["id"], investigation=investigation["id"]),
        "ReleaseTest",
    )
    moisture = effect(test(drying, 0.2), "ReleaseTest")
    certificate = effect(
        launch(
            alice,
            project,
            prefix + "prepare_certificate",
            {
                "case": case["id"],
                "tests": [original["id"], repeat["id"], moisture["id"]],
                "investigations": [investigation["id"]],
            },
        ),
        "ResearchCertificate",
    )
    report = certificate["values"]["report"]
    if (
        report["specification"]["revision"] != approved["revision"]
        or [row["value"] for row in report["tests"]] != [98.7, 99.9, 0.2]
        or report["investigations"][0]["resolution"] != "invalidated"
    ):
        fail(f"{certificate['hrid']} lost the pinned data: {report}")
    # The certificate states each criterion's final result and says it's for research,
    # without the failed attempt or its investigation.
    coa = certificate["values"]["coa"]
    if [row["value"] for row in coa["results"]] != [99.9, 0.2] or "research" not in coa.get(
        "intended_use", ""
    ):
        fail(f"{certificate['hrid']} states the wrong results: {coa}")
    sign(certificate, "review", "signed_off")
    proofs = api_call(alice, "GET", f"/entities/{certificate['id']}/signatures?verify=true").json()
    if len(proofs["items"]) != 1 or not proofs["items"][0]["verification"]["valid"]:
        fail(f"{certificate['hrid']} has no verifiable reviewer signature")
    # The certificate of analysis is issued, and signed, on its own: what's sent holds
    # only what it states, and the record keeps the evidence.
    issued = effect(
        launch(alice, project, prefix + "issue_certificate", {"certificate": certificate["id"]}),
        "CertificateOfAnalysis",
    )
    if issued["values"]["digest"] != certificate["values"]["coa_digest"]:
        fail(f"{issued['hrid']} isn't the certificate {certificate['hrid']} names")
    sign(issued, "review", "issued")
    url = f"/entities/{issued['id']}/export?format=signed-report-json"
    receipt = api_call(alice, "GET", url)
    shared = receipt.text
    if (
        receipt.status_code != 200
        or receipt.json()["report"] != coa
        or len(receipt.json()["signatures"]) != 1
        or api_call(alice, "GET", url).content != receipt.content
    ):
        fail(f"{issued['hrid']} has no reproducible signed certificate download")
    held = [original["hrid"], investigation["hrid"], certificate["hrid"], "98.7", "specimen"]
    if leaked := [text for text in held if text in shared]:
        fail(f"{issued['hrid']}'s download carries its evidence: {leaked}")
    ok(
        f"{certificate['hrid']} pins an effective specification, original OOS and signed "
        f"resolution; {issued['hrid']} states only its final results"
    )


def signing(
    session: requests.Session,
    entity: dict[str, Any],
    state: str,
    as_user: str,
    *,
    field: str = "status",
    someone_else: bool = False,
    reason: str = "",
) -> Any:  # noqa: ANN401
    """Starts signing for a state, signs in again at Keycloak as `as_user` (someone other
    than the session's person, if `someone_else`), and returns what came of it."""
    reviewed = api_call(session, "GET", f"/entities/{entity['id']}")
    entry = reviewed.json()["entry"]
    started = api_call(
        session,
        "POST",
        f"/entities/{entity['id']}/signatures",
        {
            "field": field,
            "state": state,
            "entry_revision": entry["revision"] if entry else None,
            "acknowledge": True,
            "reason": reason,
            "return_to": f"/entities/{entity['hrid']}/",
        },
        {"If-Match": reviewed.headers["ETag"]},
    )
    if started.status_code != 201:
        fail(f"signing {entity['hrid']} answered HTTP {started.status_code}: {started.text[:300]}")
    # Keycloak's own session is the signer's, and its form for signing in again keeps
    # their username, whatever is typed: to have someone else really sign in there, set
    # that session aside, as signing out of Keycloak first (or another browser) would.
    host = urlsplit(ISSUER).hostname or ""
    kept = [c for c in session.cookies if someone_else and host in c.domain]
    for cookie in kept:
        session.cookies.clear(cookie.domain, cookie.path, cookie.name)
    # Keycloak asks for the password again, though the person signed in minutes ago.
    response = keycloak_sign_in(session, session.get(IKI + started.json()["sign_in_url"]), as_user)
    if kept:
        for cookie in [c for c in session.cookies if host in c.domain]:
            session.cookies.clear(cookie.domain, cookie.path, cookie.name)
        for cookie in kept:
            session.cookies.set_cookie(cookie)
    signing_id = started.json()["id"]
    if f"signing={signing_id}" not in response.url:
        fail(f"signing in again came back to {response.url}, not the record's page")
    return api_call(session, "GET", f"/signings/{signing_id}").json()


def cell_bank_history(alice: requests.Session, project: dict[str, Any]) -> None:
    """Trace a sample's data through passage, thaw, QC and banking back to its catalogue line."""
    plugin = "iki/cell_lines."
    catalogue = Path(__file__).resolve().parents[2] / "plugins/cell_lines/tests/data/wi-38.txt"
    file_id = upload(alice, project, "WI-38.txt", catalogue.read_bytes(), "text/plain")
    sources = {}
    parent_ids = set()
    for supplier, catalogue_id in (("ATCC", "CCL-75"), ("Coriell", "AG06814-J")):
        imported = launch(
            alice,
            project,
            plugin + "import_parental_line",
            {},
            {"file": file_id, "supplier": supplier, "catalogue_id": catalogue_id},
        )
        effects = {e["type"]: e["entity"] for e in imported["effects"]}
        parent = record(alice, effects[plugin + "ParentalLine"])
        parent_ids.add(parent["id"])
        source = record(alice, effects[plugin + "CellLineSource"])
        if (
            source["values"]["supplier"],
            source["values"]["catalogue_id"],
            source["values"]["line"],
        ) != (supplier, catalogue_id, entry_of(parent)):
            fail(f"{imported['hrid']} returned unexpected supplier details {source['values']}")
        sources[supplier] = source
    if (
        len(parent_ids) != 1
        or parent["values"]["cellosaurus_id"] != "CVCL_0579"
        or parent["values"]["name"] != "WI-38"
        or sources["ATCC"]["id"] == sources["Coriell"]["id"]
    ):
        fail("The two supplier records must identify one WI-38 line")
    catalogue_graph = api_call(alice, "GET", f"/entities/{parent['id']}/graph?up=0&down=1").json()
    if not {s["id"] for s in sources.values()} <= {e["id"] for e in catalogue_graph["entities"]}:
        fail(f"{parent['hrid']}'s catalogue graph lost one of its supplier records")
    # WI-38's identification is public. All local materials, dates and results here are synthetic.
    line = created(
        alice,
        project,
        plugin + "EngineeredLine",
        {
            "name": "Synthetic reporter clone",
            "parent": entry_of(parent),
            "parent_source": entry_of(sources["ATCC"]),
            "changes": [
                {"kind": "stable_transfection", "description": "Synthetic reporter marker"}
            ],
        },
    )
    master = created(
        alice,
        project,
        plugin + "CellBank",
        {
            "name": "Synthetic master bank",
            "kind": "master",
            "line": line["id"],
            "banked_on": "2026-01-02",
            "passage_number": 8,
        },
    )
    bank = created(
        alice,
        project,
        plugin + "CellBank",
        {
            "name": "Synthetic working bank",
            "kind": "working",
            "line": line["id"],
            "source_bank": master["id"],
            "banked_on": "2026-01-06",
            "passage_number": 9,
        },
    )
    vial = created(
        alice,
        project,
        plugin + "CellVial",
        {
            "bank": bank["id"],
            "contents": bank["id"],
            "barcode": f"SYNTHETIC-{secrets.token_hex(4)}",
        },
    )
    box = created(alice, project, INVENTORY + "Box", {"name": "Synthetic cell bank"})
    moved(alice, [(vial, box, "A1")], "Synthetic working bank deposit")
    location = api_call(alice, "GET", f"/entities/{vial['id']}/location").json()
    if [(step["id"], step["position"]) for step in location["path"]] != [(box["id"], "A1")]:
        fail(f"{vial['hrid']}'s bank deposit has an unexpected location {location}")
    give_lab_roles(alice, project, plugin + "research_signer")
    checks = {}
    for kind, result in (("str_authentication", "match"), ("mycoplasma", "negative")):
        check = created(
            alice,
            project,
            plugin + "CellCheck",
            {
                "bank": bank["id"],
                "kind": kind,
                "method": "Synthetic QC report",
                "result": result,
                "checked_on": "2026-01-07",
                "due_on": "2026-02-07",
            },
        )
        signed = signing(alice, check, "signed_off", "alice", field="review")
        if signed["status"] != "signed" or not signed["set"]:
            fail(f"Research sign-off for {check['hrid']} returned {signed}")
        checks[kind] = check["id"]
    # A record-only check: the testing laboratory's report is the evidence, with no signature.
    give_lab_roles(alice, project, plugin + "recorder")
    report = upload(
        alice, project, "synthetic_sterility.csv", b"test,result\nsterility,negative\n", "text/csv"
    )
    sterility = created(
        alice,
        project,
        plugin + "CellCheck",
        {
            "bank": bank["id"],
            "kind": "sterility",
            "method": "Synthetic outside report",
            "result": "negative",
            "checked_on": "2026-01-07",
            "report": report,
        },
    )
    path = f"/entities/{sterility['id']}"
    accepted = api_call(
        alice,
        "POST",
        f"{path}/states/review",
        {"state": "accepted", "reason": ""},
        {"If-Match": api_call(alice, "GET", path).headers["ETag"]},
    )
    if accepted.status_code != 200 or record(alice, sterility["id"])["values"]["review"] != (
        "accepted"
    ):
        fail(f"accepting {sterility['hrid']} gave {accepted.status_code}: {accepted.text[:200]}")
    thaw = created(
        alice,
        project,
        plugin + "CellCulture",
        {
            "event": "thaw",
            "event_on": "2026-01-09",
            "passage_number": 9,
            "bank": bank["id"],
            "vial": vial["id"],
            "str_check": checks["str_authentication"],
            "mycoplasma_check": checks["mycoplasma"],
        },
    )
    moved(alice, [(vial, None, None)], f"Synthetic thaw recorded as {thaw['hrid']}")
    passage = created(
        alice,
        project,
        plugin + "CellCulture",
        {
            "event": "passage",
            "event_on": "2026-01-12",
            "passage_number": 10,
            "bank": bank["id"],
            "parent_culture": thaw["id"],
        },
    )
    measurement = upload(
        alice, project, "synthetic_counts.csv", b"sample,count\nsynthetic,8\n", "text/csv"
    )
    sample = created(
        alice,
        project,
        plugin + "CellSample",
        {
            "name": "Synthetic sample at passage 10",
            "collected_on": "2026-01-12",
            "culture": passage["id"],
            "data_file": measurement,
        },
    )
    graph = api_call(alice, "GET", f"/entities/{sample['id']}/graph?up=7&down=0").json()
    ancestors = {entity["id"] for entity in graph["entities"]}
    expected = {
        row["id"] for row in (passage, thaw, vial, bank, master, line, parent, sources["ATCC"])
    }
    if (
        not expected.union(checks.values()) <= ancestors
        or sources["Coriell"]["id"] in ancestors
        or sample["values"]["data_file"] != measurement
    ):
        fail(f"{sample['hrid']} lost its measurement, banking history or signed QC records")
    thaw_links = {
        edge["field"]: edge["on"]
        for edge in graph["edges"]
        if edge["node"] == thaw["id"] and edge["kind"] == "reference"
    }
    for field, kind, result in (
        ("str_check", "str_authentication", "match"),
        ("mycoplasma_check", "mycoplasma", "negative"),
    ):
        if thaw_links.get(field) != checks[kind]:
            fail(f"{thaw['hrid']}'s graph omitted the {field} it cited")
        values = record(alice, thaw_links[field])["values"]
        if (values["result"], values["due_on"], values["review"]) != (
            result,
            "2026-02-07",
            "signed_off",
        ):
            fail(f"{thaw['hrid']}'s {field} link led to unexpected QC details {values}")
    ok(
        f"{sample['hrid']} traces through P10 and {vial['hrid']}'s thaw to both signed checks; "
        f"{sterility['hrid']} is accepted with its report"
    )


def certify_dev_signatures(admin: requests.Session) -> None:
    if api_call(admin, "GET", "/signatures/certification").json()["certification"] is None:
        certified = api_call(
            admin,
            "POST",
            "/signatures/certification",
            {"submitted_on": datetime.now(UTC).date().isoformat(), "reference": "DEV-CERT-1"},
        )
        if certified.status_code != 200:
            fail(f"recording the certification answered HTTP {certified.status_code}")


def assign_reviewer(
    alice: requests.Session, witness: requests.Session, project: dict[str, Any], role: str
) -> str:
    person: str = api_call(witness, "GET", "/me").json()["id"]
    grants = f"/projects/{project['id']}/members"
    grant = api_call(alice, "PUT", f"{grants}/users/{person}", {"role": "editor"})
    if grant.status_code != 200:
        fail(f"granting the witness access answered HTTP {grant.status_code}: {grant.text}")
    roles = {r["role"] for r in grant.json()["lab_roles"]} | {role}
    assigned = api_call(
        alice, "PUT", f"{grants}/{grant.json()['id']}/lab-roles", {"roles": sorted(roles)}
    )
    if assigned.status_code != 200:
        fail(f"assigning the witness answered HTTP {assigned.status_code}: {assigned.text}")
    return person


def signatures(alice: requests.Session, project: dict[str, Any]) -> None:
    """Electronic signatures (ADR 0126) against Keycloak: submitting a sample signs in again
    there (prompt=login, its auth_time checked, ADR 0124), sets its state, shows with it and
    verifies; signing in at Keycloak as someone else gives no signature."""
    bob = sign_in_as("bob")
    give_lab_roles(
        alice,
        project,
        "iki/lab_roles.analyst",
        "iki/lab_roles.reviewer",
        "iki/lab_roles.qa",
    )
    protein = created(
        alice,
        project,
        "iki/protein.Protein",
        {"name": "Signature training protein", "sequence": "MPEPTIDE"},
    )
    tube = created(
        alice,
        project,
        "iki/inventory.Tube",
        {"name": "Signature demonstration", "contents": protein["id"]},
        {"value": 10, "unit": "uL"},
    )
    work = created(alice, project, "iki/requests.PlainRequest", {"title": "Training measurement"})
    sample = created(
        alice, project, "iki/samples.ReviewedSample", {"source": tube["id"], "purpose": work["id"]}
    )
    draw = api_call(
        alice,
        "POST",
        f"/entities/{tube['id']}/amount/draw",
        {"amount": {"value": 1, "unit": "uL"}, "used_in": sample["id"], "confirm": tube["hrid"]},
    )
    if draw.status_code != 200:
        fail(f"Drawing for {sample['hrid']} answered HTTP {draw.status_code}: {draw.text[:300]}")
    for state, reason in (("received", ""), ("in_testing", "Start testing the example sample")):
        etag = api_call(alice, "GET", f"/entities/{sample['id']}").headers["ETag"]
        moved = api_call(
            alice,
            "POST",
            f"/entities/{sample['id']}/states/status",
            {"state": state, "reason": reason},
            {"If-Match": etag},
        )
        if moved.status_code != 200:
            fail(f"setting {sample['hrid']} {state} answered HTTP {moved.status_code}")
    measured = api_call(
        alice,
        "PATCH",
        f"/entities/{sample['id']}",
        {"values": {"measurements": [{"method": "Training observation", "value": "clear"}]}},
        {"If-Match": api_call(alice, "GET", f"/entities/{sample['id']}").headers["ETag"]},
    )
    if measured.status_code != 200:
        fail(f"Recording measurements answered HTTP {measured.status_code}: {measured.text[:300]}")
    # A signed state is set only by signing for it, whoever asks.
    etag = api_call(alice, "GET", f"/entities/{sample['id']}").headers["ETag"]
    direct = api_call(
        alice,
        "POST",
        f"/entities/{sample['id']}/states/status",
        {"state": "in_review", "reason": ""},
        {"If-Match": etag},
    )
    if direct.status_code != 409 or direct.json().get("code") != "signatures_required":
        fail(f"setting {sample['hrid']} in_review directly answered HTTP {direct.status_code}")
    started = time.time()
    done = signing(alice, sample, "in_review", "alice")
    if done["status"] != "signed" or not done["set"] or done["state"] != "in_review":
        fail(f"signing {sample['hrid']} at Keycloak gave {done}")
    listed = api_call(alice, "GET", f"/entities/{sample['id']}/signatures?verify=true").json()
    [signature] = listed["items"]
    signed_in = datetime.fromisoformat(signature["authentication"]["authenticated_at"])
    if signed_in.timestamp() < started - 60:
        fail(f"Keycloak's auth_time {signed_in} is from before signing began: no fresh sign-in")
    if not signature["verification"]["valid"]:
        fail(f"{sample['hrid']}'s signature doesn't verify: {signature['verification']}")
    ok(f"{sample['hrid']}: {signature['manifestation']} (signed in again at Keycloak, verified)")
    # Returning a signed submission to testing requires a fresh sign-in by the same reviewer.
    refused = signing(
        alice,
        sample,
        "retesting",
        "bob",
        someone_else=True,
        reason="Repeat the training observation",
    )
    if refused["status"] != "failed" or "Someone else" not in refused["message"]:
        fail(f"signing in at Keycloak as bob to sign as alice gave {refused}")
    ok(f"signing in at Keycloak as bob gave no signature of alice's: {refused['message']}")
    assign_reviewer(alice, bob, project, "iki/lab_roles.reviewer")
    review = signing(bob, sample, "reviewed", "bob")
    if not review["set"]:
        fail(f"Independent review of {sample['hrid']} failed: {review}")
    path = f"/entities/{sample['id']}"
    completed = api_call(bob, "GET", path).json()
    if completed["values"]["status"] != "reviewed" or not completed["locked"]:
        fail(f"Signed review did not complete and lock {sample['hrid']}")
    carol = sign_in_as("carol")
    core = analytical_core(alice, bob, carol, project)
    analysis_result(alice, bob, carol, project, sample, core)
    core_facility_requests(alice, bob, carol, project, core)
    bench_material(alice, project, core, protein)
    cell_bank_history(alice, project)


def core_project(admin: requests.Session, asking: dict[str, Any], name: str) -> dict[str, Any]:
    team = asking["team"]["id"]
    projects: list[dict[str, Any]] = api_call(
        admin, "GET", f"/projects?team={team}&member=true&limit=100"
    ).json()["items"]
    for project in projects:
        if project["name"] == name:
            return project
    made = api_call(admin, "POST", "/projects", {"team": team, "name": name})
    if made.status_code != 201:
        fail(f"creating {name} answered HTTP {made.status_code}: {made.text[:300]}")
    core: dict[str, Any] = made.json()
    return core


def analytical_core(
    analyst: requests.Session,
    reviewer: requests.Session,
    qa: requests.Session,
    asking: dict[str, Any],
) -> dict[str, Any]:
    """Set up the team's analytical core and the people who sign its results."""
    core = core_project(analyst, asking, "Analytical Core")
    path = f"/projects/{core['id']}"
    for session, role, lab_role in (
        (analyst, "admin", "analyst"),
        (reviewer, "editor", "reviewer"),
        (qa, "editor", "qa"),
    ):
        user = api_call(session, "GET", "/me").json()["id"]
        grant = api_call(analyst, "PUT", f"{path}/members/users/{user}", {"role": role})
        if grant.status_code != 200:
            fail(f"giving a core membership answered HTTP {grant.status_code}: {grant.text[:300]}")
        given = api_call(
            analyst,
            "PUT",
            f"{path}/members/{grant.json()['id']}/lab-roles",
            {"roles": [f"iki/lab_roles.{lab_role}"]},
        )
        if given.status_code != 200:
            fail(f"giving core lab roles answered HTTP {given.status_code}: {given.text[:300]}")
    intake = api_call(
        analyst,
        "PUT",
        f"{path}/intake",
        {"types": ["iki/requests.Request"], "projects": [asking["id"]]},
    )
    if intake.status_code != 200:
        fail(f"setting the core's intake answered HTTP {intake.status_code}: {intake.text[:300]}")
    give_lab_roles(analyst, core, "iki/lab_roles.coordinator")
    return core


def start_service_work(
    analyst: requests.Session, core: dict[str, Any], request: dict[str, Any]
) -> str:
    """Send a request to the service and move it into the analyst's queue."""
    path = f"/entities/{request['id']}"
    sent = api_call(
        analyst,
        "POST",
        f"{path}/submit",
        {"service": core["id"]},
        {"If-Match": api_call(analyst, "GET", path).headers["ETag"]},
    )
    if sent.status_code != 200:
        fail(f"sending {request['hrid']} answered HTTP {sent.status_code}: {sent.text[:300]}")
    for state in ("accepted", "in_progress"):
        advanced = api_call(
            analyst,
            "POST",
            f"{path}/states/status",
            {"state": state, "reason": ""},
            {"If-Match": api_call(analyst, "GET", path).headers["ETag"]},
        )
        if advanced.status_code != 200:
            fail(
                f"setting {request['hrid']} {state} gave {advanced.status_code}: "
                f"{advanced.text[:200]}"
            )
    return path


def analysis_result(
    analyst: requests.Session,
    reviewer: requests.Session,
    qa: requests.Session,
    asking: dict[str, Any],
    sample: dict[str, Any],
    core: dict[str, Any],
) -> None:
    """A core returns the independently reviewed measurement and QA's signature to the requester."""
    request = created(
        analyst,
        asking,
        "iki/requests.QCRequest",
        {"title": "SEC purity", "materials": [sample["id"]], "analyses": ["sec_purity"]},
    )
    path = start_service_work(analyst, core, request)
    report = upload(
        analyst, core, "measurement.txt", b"Synthetic training measurement.", "text/plain"
    )
    measurement = {
        "material": sample["id"],
        "analysis": "sec_purity",
        "purity": 98.5,
        "result": "pass",
    }
    result = created(
        analyst,
        core,
        "iki/requests.QCDeliverable",
        {"request": request["id"], "report": report, "outcome": "fulfilled", **measurement},
    )
    result_path = f"/entities/{result['hrid']}"
    requester = cli_read_token(analyst, project=asking["id"])

    def submit_and_review(review_reason: str = "") -> dict[str, Any]:
        for session, name, state in (
            (analyst, "alice", "submitted"),
            (reviewer, "bob", "reviewed"),
        ):
            signed = signing(
                session,
                result,
                state,
                name,
                field="review",
                reason=review_reason if state == "reviewed" else "",
            )
            if signed["status"] != "signed" or not signed["set"]:
                fail(f"signing {result['hrid']} as {state} gave {signed}")
        return record(analyst, result["id"])

    first_review = submit_and_review()
    if not first_review["locked"] or bearer_call(requester, "GET", result_path).status_code != 404:
        fail(f"{result['hrid']} must be locked in the core until QA releases it")
    reversal = signing(
        reviewer, result, "recorded", "bob", field="review", reason="Reopen the measured purity"
    )
    if reversal["status"] != "signed" or not reversal["set"]:
        fail(f"Reopening {result['hrid']} for correction failed: {reversal}")
    reopened = record(analyst, result["id"])
    measurement["purity"] = 97.0
    edit = api_call(
        analyst,
        "PATCH",
        result_path,
        {"values": {"purity": measurement["purity"]}, "reason": "Correct the measured purity"},
        {"If-Match": f'"{reopened["revision"]}"'},
    )
    if edit.status_code != 200 or edit.json()["values"]["review"] != "recorded":
        fail(
            f"editing reviewed {result['hrid']} answered HTTP {edit.status_code}: {edit.text[:300]}"
        )
    qa_states = record(qa, result["id"])["can"]["states"]["review"]
    if next(st for st in qa_states if st["name"] == "released")["allowed"]:
        fail(f"QA could release {result['hrid']} before the correction was reviewed")
    reviewed = submit_and_review("Reviewed the corrected measured purity")
    signed = signing(qa, result, "released", "carol", field="review")
    if signed["status"] != "signed" or not signed["set"]:
        fail(f"releasing {result['hrid']} gave {signed}")
    released = bearer_call(requester, "GET", result_path).json()
    signatures = bearer_call(requester, "GET", f"{result_path}/signatures?verify=true").json()[
        "items"
    ]
    if len({s["signer"]["id"] for s in signatures}) != 3:
        fail(f"{result['hrid']} requires three distinct signers for authorship, review and release")
    if len(signatures) != 6:
        fail(
            f"{result['hrid']} must retain both submissions, reviews, the reversal and QA's release"
        )
    review = next(s for s in signatures if s["carried_by"] == reviewed["revision"])
    release = next(s for s in signatures if s["change"]["to"] == "released")
    receipt = bearer_call(requester, "GET", path + "/request").json()["result_releases"]
    if receipt != [
        {
            "entity": result["id"],
            "reviewed_revision": review["revision"],
            "signed_revision": release["revision"],
            "released_revision": released["revision"],
        }
    ]:
        fail(f"{request['hrid']} received unexpected release revisions: {receipt}")
    attested = json.loads(release["document"])["reviewed"]
    snapshot = bearer_call(
        requester, "GET", f"{result_path}/revisions/{receipt[0]['reviewed_revision']}"
    ).json()
    if (
        attested["revision"] != review["revision"]
        or attested["signatures"] != [review["id"]]
        or attested["content_sha256"] != json.loads(review["document"])["record"]["content_sha256"]
        or release["revision"] != reviewed["revision"]
        or not all(s["verification"]["valid"] for s in signatures)
        or release["carried_by"] != released["revision"]
        or not released["locked"]
        or any(
            snapshot["values"][key] != value or released["values"][key] != value
            for key, value in measurement.items()
        )
    ):
        fail(f"{result['hrid']} did not return the reviewed measurement with QA's signature")
    completed = api_call(
        reviewer,
        "POST",
        f"{path}/states/status",
        {"state": "delivered"},
        {"If-Match": api_call(reviewer, "GET", path).headers["ETag"]},
    )
    if completed.status_code != 200 or completed.json()["values"]["status"] != "delivered":
        fail(f"{request['hrid']} could not close after QA release: {completed.text[:300]}")
    ok(f"{result['hrid']}: QA released corrected revision {review['revision']} to the requester")


def core_facility_requests(
    analyst: requests.Session,
    reviewer: requests.Session,
    qa: requests.Session,
    asking: dict[str, Any],
    core: dict[str, Any],
) -> None:
    """The working core returns production and sequencing deliverables through Requests."""
    give_lab_roles(analyst, core, "iki/lab_roles.coordinator")
    example = Path(__file__).resolve().parents[2] / "examples" / "core-facility-requests"
    # The production transaction is simulated; only the registered UniProt identity is public.
    fasta = (example / "P00330.fasta").read_text().splitlines()
    protein = created(
        analyst,
        asking,
        "iki/protein.Protein",
        {
            "name": "Yeast alcohol dehydrogenase 1",
            "sequence": "".join(fasta[1:]),
            "uniprot_accession": "P00330",
        },
    )
    # The tube handoff is simulated; the public ENA run is for yeast isolate CEY652.
    tube = created(analyst, asking, INVENTORY + "Tube", {"name": "Training CEY652"})
    requester = cli_read_token(analyst, project=asking["id"])
    for prefix, filename, values, evidence, steps in (
        (
            "Research",
            "P00330-production.txt",
            {
                "title": "Training: ADH1 production",
                "facility_service": "protein_production",
                "records": [protein["id"]],
                "requested_mass": 1,
            },
            {"protein": protein["id"], "lot_number": "TRAINING-ADH1-01", "delivered_mass": 0.8},
            ((reviewer, "bob", "signed_off"),),
        ),
        (
            "QC",
            "ERR13376132-delivery.txt",
            {
                "title": "Training: long-read sequencing",
                "facility_service": "sequencing",
                "sequencing_method": "long_read",
                "tubes": [tube["id"]],
            },
            {"material": tube["id"], "run_accession": "ERR13376132"},
            (
                (analyst, "alice", "submitted"),
                (reviewer, "bob", "reviewed"),
                (qa, "carol", "released"),
            ),
        ),
    ):
        request = created(analyst, asking, f"iki/requests.{prefix}Request", values)
        path = start_service_work(analyst, core, request)
        report = upload(analyst, core, filename, (example / filename).read_bytes(), "text/plain")
        delivery = created(
            analyst,
            core,
            f"iki/requests.{prefix}Deliverable",
            {"request": request["id"], "report": report, "outcome": "fulfilled", **evidence},
        )
        if bearer_call(requester, "GET", f"/files/{report}").status_code != 404:
            fail(f"{delivery['hrid']}'s report was visible before signing")
        for signer, name, state in steps:
            done = signing(signer, delivery, state, name, field="review")
            if done["status"] != "signed" or not done["set"]:
                fail(f"signing {delivery['hrid']} as {state} gave {done}")
        received = bearer_call(requester, "GET", path + "/request").json()["result_releases"]
        if [item["entity"] for item in received] != [delivery["id"]]:
            fail(f"{request['hrid']} didn't receive {delivery['hrid']}: {received}")
        if bearer_call(requester, "GET", f"/files/{report}").status_code != 200:
            fail(f"{delivery['hrid']}'s report wasn't delivered to the requester")
        ok(
            f"{request['hrid']}: {delivery['hrid']} returned its report "
            f"with {len(steps)} signatures"
        )
        if prefix == "QC":
            booked_for_the_analyst(analyst, reviewer, core, request)
        if prefix == "Research":
            plain = created(
                analyst, asking, "iki/requests.PlainRequest", {"title": "Training handoff"}
            )
            start_service_work(analyst, core, plain)
            work_and_deliverable(analyst, reviewer, requester, core, plain, report)


def booked_for_the_analyst(
    coordinator: requests.Session,
    analyst: requests.Session,
    core: dict[str, Any],
    request: dict[str, Any],
) -> None:
    """The core's coordinator books its sequencer for the analyst who runs the request:
    it's on the analyst's home and the request's lineage, and on the coordinator's home
    among the bookings she made for others, not her own."""
    who = api_call(analyst, "GET", "/me").json()["id"]
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    sequencer = created(
        coordinator, core, "iki/equipment.Instrument", {"name": f"PromethION {stamp}"}
    )
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    booking = created(
        coordinator,
        core,
        "iki/equipment.Booking",
        {
            "purpose": "Long-read sequencing run",
            "instrument": sequencer["id"],
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=48)).isoformat(),
            "booked_by": who,
            "work": request["id"],
        },
    )
    this = {"field": "hrid", "op": "eq", "value": booking["hrid"]}

    def home(session: requests.Session, component: str) -> list[dict[str, Any]]:
        path = f"/components/iki/equipment.{component}/entities"
        found = api_call(session, "POST", path, {"home": True, "refine": {"where": this}})
        if found.status_code != 200:
            fail(f"{component} on a home answered HTTP {found.status_code}: {found.text[:300]}")
        items: list[dict[str, Any]] = found.json()["items"]
        return items

    theirs = home(analyst, "booking_calendar")
    if [i["hrid"] for i in theirs] != [booking["hrid"]]:
        fail(f"the analyst's home shows {[i['hrid'] for i in theirs]}, not {booking['hrid']}")
    if theirs[0]["refs"].get(request["id"], {}).get("hrid") != request["hrid"]:
        fail(f"{booking['hrid']} on the analyst's home doesn't say it's for {request['hrid']}")
    if home(coordinator, "booking_calendar"):
        fail(f"the coordinator's own bookings include {booking['hrid']}, made for the analyst")
    made = home(coordinator, "bookings_made")
    if [i["hrid"] for i in made] != [booking["hrid"]] or who not in made[0]["people"]:
        fail(f"the coordinator's bookings for others show {made}")
    lineage = api_call(analyst, "GET", f"/entities/{request['id']}/downstream")
    if booking["id"] not in {i["entity"]["id"] for i in lineage.json()["items"]}:
        fail(f"{request['hrid']}'s lineage doesn't list {booking['hrid']}: {lineage.text[:300]}")
    ok(
        f"{booking['hrid']}, booked by the core's coordinator for the analyst running "
        f"{request['hrid']}, is on the analyst's home and the request's lineage"
    )


def work_and_deliverable(
    analyst: requests.Session,
    reviewer: requests.Session,
    requester: str,
    service: dict[str, Any],
    request: dict[str, Any],
    report: str,
) -> None:
    """Seed the work that stays with the service and the result the asking team receives."""
    work = created(
        analyst,
        service,
        "iki/requests.WorkItem",
        {
            "request": request["id"],
            "priority": "high",
            "scheduled_on": "2026-11-24",
            "instrument": "Training bench",
            "notes": "Prepare the workspace and check the handoff.",
        },
    )
    work_path = f"/entities/{work['id']}"
    if api_call(analyst, "GET", work_path).status_code != 200:
        fail(f"the service could not read work item {work['hrid']}")
    if bearer_call(requester, "GET", work_path).status_code != 404:
        fail(f"work item {work['hrid']} was visible to the team that asked")

    result = created(
        analyst,
        service,
        "iki/requests.PlainDeliverable",
        {"request": request["id"], "report": report, "outcome": "fulfilled"},
    )
    path = f"/entities/{result['id']}"
    if bearer_call(requester, "GET", path).status_code != 404:
        fail(f"{result['hrid']} was visible before it was delivered")
    etag = api_call(reviewer, "GET", path).headers["ETag"]
    delivered = api_call(
        reviewer, "POST", f"{path}/states/status", {"state": "delivered"}, {"If-Match": etag}
    )
    if delivered.status_code != 200:
        fail(
            f"delivering {result['hrid']} gave HTTP {delivered.status_code}: {delivered.text[:300]}"
        )
    if bearer_call(requester, "GET", path).status_code != 200:
        fail(f"{result['hrid']} was not visible after it was delivered")
    ok(
        f"{work['hrid']} stayed in the service project and "
        f"{result['hrid']} reached the team that asked"
    )


INVENTORY = "iki/inventory."


def moved(
    session: requests.Session, items: list[tuple[Any, Any, str | None]], reason: str
) -> dict[str, Any]:
    """Moves records, all or none (ADR 0180)."""
    answer = api_call(session, "POST", "/storage/moves", moves_body(items, reason))
    if answer.status_code != 200:
        fail(f"moving {len(items)} records answered HTTP {answer.status_code}: {answer.text[:300]}")
    done: dict[str, Any] = answer.json()
    return done


def moves_body(items: list[tuple[Any, Any, str | None]], reason: str) -> dict[str, Any]:
    return {
        "items": [
            {"record": r["id"], "to": t["id"] if t else None, "position": p} for r, t, p in items
        ],
        "reason": reason,
    }


def bench_material(
    alice: requests.Session,
    project: dict[str, Any],
    core: dict[str, Any],
    protein: dict[str, Any],
) -> None:
    """Material at the bench (ADR 0180): a freezer, shelf, rack and box of tubes, each
    position checked; a draw scanned and linked to what it went into and its lot's guards
    refusing its tubes; tubes sent with a request, received against it at the core and put
    away; the core's box reconciled by scan; and labels as ZPL."""
    tag = secrets.token_hex(3).upper()
    freezer = created(
        alice,
        project,
        f"{INVENTORY}Freezer",
        {
            "name": "-80 °C freezer 3",
            "asset_tag": f"FRZ-{tag}",
            "set_point_c": -80,
            "room": "B2.14",
        },
    )
    shelf = created(alice, project, f"{INVENTORY}Shelf", {"name": "Shelf 2"})
    rack = created(alice, project, f"{INVENTORY}Rack", {"name": "Rack C"})
    box = created(
        alice,
        project,
        f"{INVENTORY}Box",
        {"name": "Kinase retains", "rows": 9, "columns": 9, "contents": "Lot release retains"},
    )
    # A lot in quarantine, which only QA samples; one past its expiry, which nobody draws from.
    give_lab_roles(alice, project, "iki/lots.qa")
    quarantined = created(
        alice,
        project,
        "iki/lots.ProteinLot",
        {"protein": entry_of(protein), "lot_number": f"KIN-{tag}-1"},
        {"value": 5, "unit": "mg"},
    )
    expired = created(
        alice,
        project,
        "iki/lots.ProteinLot",
        {
            "protein": entry_of(protein),
            "lot_number": f"KIN-{tag}-0",
            "made_on": "2024-01-10",
            "expires_on": "2025-01-10",
        },
        {"value": 5, "unit": "mg"},
    )
    named = (
        ("Kinase 2 mg/mL", None),
        ("Kinase QC", quarantined),
        ("Kinase 2024", expired),
        ("Kinase ship A", None),
        ("Kinase ship B", None),
    )
    tubes = [
        created(
            alice,
            project,
            f"{INVENTORY}Tube",
            {"name": name, "barcode": f"{tag}{i:04}", "contents": (lot or protein)["id"]},
            {"value": 200, "unit": "uL"},
        )
        for i, (name, lot) in enumerate(named)
    ]
    moved(
        alice,
        [(shelf, freezer, None), (rack, shelf, None), (box, rack, "3")]
        + [(t, box, None) for t in tubes],
        "Set up freezer 3",
    )
    where = api_call(alice, "GET", f"/entities/{tubes[2]['id']}/location").json()
    path = [(s["hrid"], s["position"]) for s in where["path"]]
    expected = [
        (freezer["hrid"], None),
        (shelf["hrid"], None),
        (rack["hrid"], "3"),
        (box["hrid"], "A3"),
    ]
    if path != expected:
        fail(f"{tubes[2]['hrid']} is at {path}, not {expected}")
    clash = api_call(alice, "POST", "/storage/moves", moves_body([(tubes[0], box, "A3")], "Swap"))
    outside = api_call(alice, "POST", "/storage/moves", moves_body([(tubes[0], box, "J1")], "Out"))
    if clash.status_code != 422 or outside.status_code != 422:
        fail(f"a taken position answered {clash.status_code}, one outside {outside.status_code}")
    ok(f"{tubes[2]['hrid']} is in freezer 3, shelf 2, rack slot 3, at A3, where nothing else goes")
    # A draw scans the tube, and names what it went into.
    experiment = created(
        alice, project, "iki/requests.PlainRequest", {"title": "Kinase SEC, run 12"}
    )
    draw = f"/entities/{tubes[0]['id']}/amount/draw"
    body = {"amount": {"value": 20, "unit": "uL"}, "reason": "SEC", "used_in": experiment["id"]}
    unscanned = api_call(alice, "POST", draw, body)
    drawn = api_call(alice, "POST", draw, {**body, "confirm": tubes[0]["values"]["barcode"]})
    if unscanned.status_code != 422 or drawn.status_code != 200:
        fail(f"drawing unscanned answered {unscanned.status_code}, scanned {drawn.status_code}")
    movement = drawn.json()["movement"]
    if movement["used_in"] != experiment["id"] or drawn.json()["amount"]["value"] != "180":
        fail(f"{tubes[0]['hrid']}'s draw didn't say what it went into: {movement}")
    # A tube of a quarantined lot gives only a QA sample; of an expired one, nothing.
    qc = f"/entities/{tubes[1]['id']}/amount/draw"
    scan = {"confirm": tubes[1]["values"]["barcode"], "amount": {"value": 5, "unit": "uL"}}
    routine = api_call(alice, "POST", qc, scan)
    sampled = api_call(alice, "POST", qc, {**scan, "purpose": "qa_sampling", "reason": "QC"})
    old = api_call(
        alice,
        "POST",
        f"/entities/{tubes[2]['id']}/amount/draw",
        {"confirm": tubes[2]["values"]["barcode"], "amount": {"value": 5, "unit": "uL"}},
    )
    if (routine.status_code, sampled.status_code, old.status_code) != (422, 200, 422):
        fail(
            f"a quarantined lot's tube answered {routine.status_code} routine and "
            f"{sampled.status_code} sampled; an expired one's {old.status_code}"
        )
    if sampled.json()["movement"]["source"] != quarantined["id"] or "expiry" not in old.text:
        fail(f"a lot's guards weren't its tubes': {sampled.text[:200]} {old.text[:200]}")
    ok(
        f"{tubes[0]['hrid']}'s draw was scanned and went into {experiment['hrid']}; "
        f"{quarantined['hrid']}'s tube gave only a QA sample, {expired['hrid']}'s nothing"
    )
    receive_at_core(alice, project, core, tubes[3:])
    labels = api_call(
        alice,
        "POST",
        "/storage/labels",
        {"records": [box["id"], tubes[0]["id"]], "size": "standard", "format": "zpl"},
    )
    if labels.status_code != 200 or labels.json()["zpl"].count("^BXN") != 2:
        fail(f"labels answered HTTP {labels.status_code}: {labels.text[:300]}")
    ok(f"labels for {box['hrid']} and {tubes[0]['hrid']} came as ZPL with DataMatrix codes")


def read_completed_receipt(alice: requests.Session, receipt_id: str) -> dict[str, Any]:
    got = api_call(alice, "GET", f"/receipts/{receipt_id}")
    if got.status_code != 200:
        fail(f"reading receipt {receipt_id} answered HTTP {got.status_code}: {got.text[:300]}")
    receipt: dict[str, Any] = got.json()
    if receipt["status"] != "completed":
        fail(f"receipt {receipt_id} has status {receipt['status']}")
    return receipt


def receive_at_core(
    alice: requests.Session,
    project: dict[str, Any],
    core: dict[str, Any],
    tubes: list[dict[str, Any]],
) -> None:
    """Tubes sent with a request, received at the core against it, put away and counted."""
    give_lab_roles(alice, core, "iki/lab_roles.coordinator")
    incoming = created(
        alice, core, f"{INVENTORY}Box", {"name": "Incoming", "rows": 2, "columns": 5}
    )
    request = created(
        alice,
        project,
        "iki/requests.PlainRequest",
        {"title": "Kinase intact mass", "tubes": [t["id"] for t in tubes]},
    )
    path = f"/entities/{request['id']}"
    sent = api_call(
        alice,
        "POST",
        f"{path}/submit",
        {"service": core["id"]},
        {"If-Match": api_call(alice, "GET", path).headers["ETag"]},
    )
    if sent.status_code != 200:
        fail(f"sending {request['hrid']} answered HTTP {sent.status_code}: {sent.text[:300]}")
    started = api_call(alice, "POST", f"{path}/receipts")
    if started.status_code != 201:
        fail(
            f"receiving {request['hrid']} answered HTTP {started.status_code}: {started.text[:300]}"
        )
    receipt = started.json()
    at = f"/receipts/{receipt['id']}"
    lines = {x["code"]: x["id"] for x in receipt["lines"]}
    first, second = (t["values"]["barcode"] for t in tubes)
    scanned = api_call(alice, "POST", f"{at}/scan", {"code": first})
    if scanned.status_code != 200 or scanned.json()["found"] != "matched":
        fail(f"scanning {first} answered {scanned.status_code}: {scanned.text[:300]}")
    damaged = api_call(
        alice,
        "PATCH",
        f"{at}/lines/{lines[second]}",
        {"outcome": "damaged", "note": "Cap cracked; contents frozen"},
    )
    if damaged.status_code != 200 or damaged.json().get("found") != "recorded":
        fail(f"recording damage to {second} answered {damaged.status_code}: {damaged.text[:300]}")
    described = api_call(
        alice,
        "PATCH",
        at,
        {
            "handed_over_by": "Courier, waybill 1Z-DEV",
            "note": "Dry ice present",
            "plan_revision": damaged.json()["receipt"]["plan_revision"],
        },
    )
    if described.status_code != 200:
        fail(f"recording the handover answered {described.status_code}: {described.text[:300]}")
    incoming_code = "IKI:" + UUID(incoming["id"]).hex.upper()
    done = api_call(
        alice,
        "POST",
        f"{at}/complete",
        {
            "put_away": [
                {"line": lines[first], "to": incoming_code, "position": "A1"},
                {"line": lines[second], "to": incoming_code, "position": "A2"},
            ],
            "plan_revision": described.json()["plan_revision"],
        },
    )
    if done.status_code != 200:
        fail(
            f"completing {request['hrid']}'s receipt answered {done.status_code}: {done.text[:300]}"
        )
    received = read_completed_receipt(alice, receipt["id"])
    outcomes = received["outcomes"]
    if (outcomes["matched"], outcomes["damaged"]) != (1, 1):
        fail(f"{request['hrid']}'s receipt counted {outcomes}")
    placed = {
        line["entity"]: (line["outcome"], line["put_away"]["id"], line["put_away"]["position"])
        for line in received["lines"]
        if line["put_away"] and not line["received_unplaced"]
    }
    if len(received["lines"]) != 2 or placed != {
        tubes[0]["id"]: ("matched", incoming["id"], "A1"),
        tubes[1]["id"]: ("damaged", incoming["id"], "A2"),
    }:
        fail(f"{request['hrid']}'s receipt has the wrong tube placements: {received['lines']}")
    contents = api_call(alice, "GET", f"/storage/containers/{incoming['id']}").json()["contents"]
    if [(c["id"], c["position"]) for c in contents] != [
        (tubes[0]["id"], "A1"),
        (tubes[1]["id"], "A2"),
    ]:
        fail(f"{incoming['hrid']} did not receive the expected tubes at A1 and A2: {contents}")
    ok(f"the core received {request['hrid']}'s tubes, one damaged, put away in {incoming['hrid']}")

    def review(plan: dict[str, Any], decision: str | None) -> tuple[str, int]:
        """Check the two tubes and the empty slot before approving this version."""
        lines = plan["lines"]
        actual = {
            (line["entity"], line["expected"], line["found"], line["finding"], line["resolution"])
            for line in lines
        }
        expected = {
            (tubes[0]["id"], "A1", "A1", "matched", None),
            (tubes[1]["id"], "A2", "B1", "wrong_slot", decision),
            (None, None, "A2", "empty", None),
        }
        if (
            plan["container"]["id"] != incoming["id"]
            or plan["status"] != "open"
            or actual != expected
            or any(line["resolution_stale"] for line in lines)
        ):
            fail(f"unexpected reconciliation plan for {incoming['hrid']}: {lines}")
        wrong = next(line for line in lines if line["entity"] == tubes[1]["id"])
        return str(wrong["id"]), int(wrong["line_revision"])

    # Counting the core's box: one where it should be, the other found in the wrong position.
    started = api_call(alice, "POST", f"/storage/containers/{incoming['id']}/reconciliations")
    if started.status_code != 201:
        fail(
            f"starting a count for {incoming['hrid']} answered {started.status_code}: "
            f"{started.text[:300]}"
        )
    count = f"/reconciliations/{started.json()['id']}"
    scans = [
        {"position": "A1", "code": first},
        {"position": "A2", "code": None},
        {"position": "B1", "code": second},
    ]
    observed = api_call(alice, "POST", f"{count}/scans", {"scans": scans})
    if observed.status_code != 200:
        fail(f"scanning {incoming['hrid']} answered {observed.status_code}: {observed.text[:300]}")
    found = observed.json()
    wrong, line_revision = review(found, None)
    resolved = api_call(
        alice,
        "PATCH",
        f"{count}/lines/{wrong}",
        {"resolution": "moved", "line_revision": line_revision},
    )
    if resolved.status_code != 200:
        fail(f"resolving the move to B1 answered {resolved.status_code}: {resolved.text[:300]}")
    plan = resolved.json()
    review(plan, "moved")
    finished = api_call(
        alice,
        "POST",
        f"{count}/complete",
        {"note": "Weekly count", "plan_revision": plan["plan_revision"]},
    )
    if finished.status_code != 200:
        fail(
            f"reconciling {incoming['hrid']} answered {finished.status_code}: {finished.text[:300]}"
        )
    contents = api_call(alice, "GET", f"/storage/containers/{incoming['id']}").json()["contents"]
    if [(c["id"], c["position"]) for c in contents] != [
        (tubes[0]["id"], "A1"),
        (tubes[1]["id"], "B1"),
    ]:
        fail(f"after reconciling, {incoming['hrid']} holds {contents}")
    ok(f"reconciling {incoming['hrid']} by scan moved {tubes[1]['hrid']} from A2 to B1")


#: Where the booking calendar's places get their bookings (ADR 0117).
CALENDAR = "/components/iki/equipment.booking_calendar/entities"


def bookings(alice: requests.Session, project: dict[str, Any]) -> None:
    """The equipment plugin's booking calendar (ADR 0117): each of its places gets the
    bookings its search finds, computed by the server as the person may read them, with
    only the fields it reads; a refinement only narrows; an app can place it; and someone
    outside the project gets nothing."""

    def made(kind: str, values: dict[str, Any]) -> dict[str, Any]:
        body = {"type": f"iki/equipment.{kind}", "project": project["id"], "values": values}
        response = api_call(alice, "POST", "/entities", body)
        if response.status_code != 201:
            fail(f"making a {kind} answered HTTP {response.status_code}: {response.text[:300]}")
        found: dict[str, Any] = response.json()
        return found

    def shown(session: requests.Session, place: dict[str, Any]) -> requests.Response:
        return api_call(session, "POST", CALENDAR, place)

    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    reader, other = (made("Instrument", {"name": f"{n} {stamp}"}) for n in ("Reader", "qPCR"))
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    booked = [
        made(
            "Booking",
            {
                "purpose": purpose,
                "instrument": entry_of(instrument),
                "starts_at": (start + timedelta(hours=hours)).isoformat(),
                "ends_at": (start + timedelta(hours=hours + 2)).isoformat(),
            },
        )
        for purpose, instrument, hours in (
            ("Kinase assay", reader, 2),
            ("Thermal shift", reader, 26),
            ("Melt curve", other, 3),
        )
    ]
    tab = shown(alice, {"entity": reader["id"]})
    if tab.status_code != 200:
        fail(f"the calendar's tab answered HTTP {tab.status_code}: {tab.text[:300]}")
    items = tab.json()["items"]
    if [i["hrid"] for i in items] != [b["hrid"] for b in booked[:2]]:
        fail(f"{reader['hrid']}'s calendar shows {[i['hrid'] for i in items]}, not its bookings")
    reads = {"purpose", "instrument", "starts_at", "ends_at", "booked_by", "work", "cancelled"}
    if not {"purpose", "instrument", "starts_at", "ends_at"} <= set(items[0]["values"]) <= reads:
        fail(f"the calendar was given {sorted(items[0]['values'])}, not the fields it reads")
    if items[0]["refs"].get(entry_of(reader), {}).get("hrid") != reader["hrid"]:
        fail(f"the calendar wasn't given {reader['hrid']}, which its bookings name")
    day = {
        "all": [
            {"field": "starts_at", "op": "gte", "value": start.isoformat()},
            {"field": "starts_at", "op": "lt", "value": (start + timedelta(days=1)).isoformat()},
        ]
    }
    narrowed = shown(alice, {"entity": reader["id"], "refine": {"where": day}}).json()["items"]
    if [i["hrid"] for i in narrowed] != [booked[0]["hrid"]]:
        fail(f"a day of {reader['hrid']}'s calendar shows {[i['hrid'] for i in narrowed]}")
    wider = shown(alice, {"entity": reader["id"], "refine": {"where": {"field": "name"}}})
    if wider.status_code != 422:
        fail(f"a refinement on a field the calendar doesn't read answered {wider.status_code}")
    # Her home has every booking she made, those of earlier runs too: these, by their IDs.
    these = {"field": "hrid", "op": "in", "value": [b["hrid"] for b in booked]}
    home = shown(alice, {"home": True, "refine": {"where": these}}).json()["items"]
    if sorted(i["hrid"] for i in home) != sorted(b["hrid"] for b in booked):
        fail(f"Alice's home calendar shows {[i['hrid'] for i in home]} of the bookings she made")
    ok(
        f"the booking calendar shows {reader['hrid']}'s bookings on its tab, a day of them "
        "when it narrows, and Alice's on her home, with only the fields it reads"
    )
    # An app places it beside Iki's own grid, given the instrument picked there.
    spec = {
        "version": 1,
        "panels": [
            {
                "id": "instruments",
                "component": "iki.entities",
                "props": {"type": "iki/equipment.Instrument", "sort": "name"},
            },
            {
                "id": "calendar",
                "title": "Bookings",
                "component": "iki/equipment.booking_calendar",
                "props": {"entity": "{{ instruments.selection }}"},
            },
        ],
    }
    saved = save_app(alice, project, "Instrument bookings", spec)
    if saved.status_code not in (200, 201):
        fail(f"saving an app with the calendar answered HTTP {saved.status_code}: {saved.text}")
    ok(f"an app places the booking calendar beside Iki's grid ({saved.json()['hrid']})")
    # Bob, an Org Admin but not in the project, gets none of it.
    bob = sign_in_as("bob")
    seen = (
        shown(bob, {"entity": reader["id"]}).status_code,
        shown(bob, {"project": project["id"]}).status_code,
        [i["hrid"] for i in shown(bob, {"home": True, "refine": {"where": these}}).json()["items"]],
    )
    if seen != (404, 403, []):
        fail(f"Bob, outside {project['name']}, got {seen} from the calendar, not nothing")
    ok("someone outside the project gets none of its bookings from the calendar")


#: Codons for amino acids alone (no stops), to make DNA that translates whole.
SENSE = [a + b + c for a in "ACGT" for b in "ACGT" for c in "ACGT"]
SENSE = [codon for codon in SENSE if codon not in ("TAA", "TAG", "TGA")]


def launch(
    alice: requests.Session,
    project: dict[str, Any],
    action: str,
    inputs: dict[str, str | list[str]],
    params: dict[str, Any] | None = None,
    within: int = 120,
) -> dict[str, Any]:
    body = {"project": project["id"], "inputs": inputs, "params": params or {}}
    launched = api_call(alice, "POST", f"/actions/{action}/runs", body)
    if launched.status_code != 201:
        fail(f"launching {action} answered HTTP {launched.status_code}: {launched.text[:300]}")
    run = followed(alice, launched.json(), within=within)
    if run["status"] != "succeeded":
        fail(f"{run['hrid']} ({action}) {run['status']}: {run['error']}")
    return run


def proteins(alice: requests.Session, project: dict[str, Any]) -> None:
    """The protein plugin's actions, in its environment with Biopython: DNA translated into
    a protein linked to it, then the protein's properties."""
    # New bases each time, so each check registers a new protein.
    bases = "ATG" + "".join(secrets.choice(SENSE) for _ in range(29)) + "TAA"
    made = api_call(
        alice,
        "POST",
        "/entities",
        {"type": "iki/dna.DNASequence", "project": project["id"], "values": {"bases": bases}},
    )
    if made.status_code not in (200, 201):
        fail(f"registering a DNA sequence answered HTTP {made.status_code}: {made.text[:300]}")
    dna = made.json()
    run = launch(alice, project, "iki/protein.translate", {"sequence": dna["id"]})
    created = [e for e in run["effects"] if e["kind"] in ("created", "reused")]
    if [e["type"] for e in created] != ["iki/protein.Protein"]:
        fail(f"{run['hrid']} (iki/protein.translate) made {run['effects']}, not one protein")
    protein = record(alice, created[0]["entity"])
    values = protein["values"]
    if len(values.get("sequence", "")) != 30 or values.get("encoded_by") != entry_of(dna):
        fail(f"{run['hrid']} made {values}, not 30 residues encoded by {dna['hrid']}")
    ok(f"iki/protein.translate made {protein['hrid']} from {dna['hrid']} ({run['hrid']})")
    run = launch(alice, project, "iki/protein.compute_properties", {"protein": protein["id"]})
    got = api_call(alice, "GET", f"/entities/{protein['id']}").json()["values"]
    weight = got.get("molecular_weight")
    point = got.get("isoelectric_point")
    if (
        got.get("length") != 30
        or not isinstance(weight, (int, float))
        or not isinstance(point, (int, float))
        or not 0 < point < 14
    ):
        fail(f"{run['hrid']} wrote {got}, not a length, molecular weight and pI")
    ok(
        f"iki/protein.compute_properties ran in Biopython's environment ({run['hrid']}): "
        f"{weight} Da, pI {point}"
    )
    chains = aligned(alice, project)
    reads_on_protein(alice, project, chains[1])
    aligned_many(alice, project, chains)
    globins_app(alice, project)
    dashboard(alice, project, relatives_app(alice, project))
    app_draft(alice, project)
    structures(alice, project)
    alphafold(alice, project)
    antibodies(alice, project)


#: The example models plugin's outside API (examples/plugins/hosted_models), and where the dev
#: cluster's settings point it: the fake FoldCloud (fixtures/fake-foldcloud.yaml).
FOLDCLOUD = "acme/hosted_models.foldcloud"
FOLDCLOUD_HOST = "fake-foldcloud.iki.svc"
#: What the CI fixture charges for its calls, in USD: per call, and per GPU second
#: the finished job reports. The fake reports half a GPU second per residue.
FOLDCLOUD_PER_CALL = 0.01
FOLDCLOUD_PER_GPU_SECOND = 0.0011


def foldcloud(alice: requests.Session, project: dict[str, Any]) -> None:
    """A model behind an outside API (ADR 0130): refused until an Org Admin approves
    FoldCloud for Atlas, then a prediction that calls it through the egress proxy, which
    adds the organization's key, parks on its job without a worker, wakes on its signed
    webhook and resumes to import the structure. The proxy's record of each call says what
    answered, and their prices are the run's cost. The approval is revoked afterwards."""
    bob = sign_in_as("bob")
    protein = created(
        alice,
        project,
        "iki/protein.Protein",
        {"name": "FoldCloud test input", "sequence": "AGSGAGSGAGSG"},
    )
    approvals = f"/outside-apis/{FOLDCLOUD}/approvals"
    # One left by an earlier check that stopped part way.
    api_call(bob, "DELETE", f"{approvals}/{project['id']}")
    action = "acme/hosted_models.predict_structure"
    body = {"project": project["id"], "inputs": {"protein": protein["id"]}, "params": {}}
    refused = api_call(alice, "POST", f"/actions/{action}/runs", {**body, "consent": [FOLDCLOUD]})
    if refused.status_code != 409 or refused.json().get("code") != "outside_api_not_approved":
        fail(f"launching {action} unapproved answered {refused.status_code}: {refused.text[:300]}")
    approved = api_call(bob, "POST", approvals, {"project": project["id"], "note": "e2e"})
    if approved.status_code != 201 or approved.json()["host"] != FOLDCLOUD_HOST:
        fail(f"bob approving FoldCloud answered {approved.status_code}: {approved.text[:300]}")
    try:
        unconsented = api_call(alice, "POST", f"/actions/{action}/runs", body)
        if unconsented.status_code != 409 or unconsented.json().get("code") != "consent_required":
            fail(f"launching {action} without consent answered {unconsented.status_code}")
        launched = api_call(
            alice, "POST", f"/actions/{action}/runs", {**body, "consent": [FOLDCLOUD]}
        )
        if launched.status_code != 201:
            fail(f"launching {action} answered {launched.status_code}: {launched.text[:300]}")
        run = followed(alice, launched.json(), within=180)
        if run["status"] != "succeeded":
            fail(f"{run['hrid']} ({action}) {run['status']}: {run['error']}")
        park = run["park"] or {}
        if park.get("api") != "foldcloud" or park.get("outcome") != "done":
            fail(f"{run['hrid']} didn't park on FoldCloud's job and resume: {park}")
        # The request log is written once Iki answers the callback.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            callbacks = requests.get(FOLDCLOUD_REQUESTS["url"], timeout=5).json()
            if any(
                event["method"] == "CALLBACK"
                and event["job"] == park["job"]
                and 200 <= event["status"] < 300
                for event in callbacks
            ):
                break
            time.sleep(0.5)
        else:
            fail(f"FoldCloud did not deliver the signed callback for {park['job']}")
        attempts = api_call(alice, "GET", f"/runs/{run['id']}/attempts").json()
        if [(a["attempt"], a["state"]) for a in attempts] != [(1, "parked"), (2, "succeeded")]:
            fail(f"{run['hrid']}'s attempts did not park and resume: {attempts}")
        me = api_call(alice, "GET", "/me").json()
        consent = run["consent"] or {}
        if (
            (consent.get("given_by") or {}).get("id") != me["id"]
            or [s["api"] for s in consent.get("statements", [])] != [FOLDCLOUD]
            or [a["host"] for a in run["apis"]] != [FOLDCLOUD_HOST]
        ):
            fail(f"{run['hrid']} doesn't say alice agreed to what it sent where: {consent}")
        made = [e for e in run["effects"] if e["type"] == "iki/protein.ProteinStructure"]
        if len(made) != 1:
            fail(f"{run['hrid']} made {run['effects']}, not one structure")
        values = api_call(alice, "GET", f"/entities/{made[0]['entity']}").json()["values"]
        if (
            values.get("method") != "predicted"
            or values.get("chains") != [entry_of(protein)]
            # The fake's helix is chain A, so its secondary structure can be by chain.
            or values.get("chain_ids") != ["A"]
            or values.get("name") != f"{protein['hrid']}, predicted by fold-2"
            or not values.get("structure")
        ):
            fail(f"{run['hrid']} made a structure with {values}")
        origins = api_call(alice, "GET", f"/entities/{made[0]['entity']}/origins").json()
        said = [o for o in origins if o["source"] == "FoldCloud"]
        # The API gives each software its version, empty here: the model's version is in the
        # run's call record, not on the structure.
        software = (said[-1]["made"] or {}).get("software") if said else None
        if [s.get("name") for s in software or []] != ["FoldCloud fold-2"]:
            fail(f"{run['hrid']}'s structure doesn't say FoldCloud's fold-2 made it: {origins}")
        calls = api_call(alice, "GET", f"/runs/{run['id']}/api-calls?limit=100").json()["items"]
        submitted = [c for c in calls if c["kind"] == "call"]
        finished = [c for c in calls if c["kind"] == "poll" and c["usage"]]
        gpu_seconds = len(protein["values"]["sequence"]) * 0.5
        if (
            [(c["method"], c["path"], c["status"]) for c in submitted]
            != [("POST", "/v1/predictions", 201)]
            or {c["host"] for c in calls} != {FOLDCLOUD_HOST}
            or (submitted[0]["model"], submitted[0]["version"]) != ("fold-2", "2.1.0")
            or submitted[0]["parameters"] != {"/model": "fold-2", "/num_recycles": 3}
            or not all(len(c["request_sha256"]) == 64 for c in calls)
            or [c["usage"] for c in finished] != [{"gpu_seconds": gpu_seconds}]
        ):
            fail(f"{run['hrid']}'s calls to FoldCloud were recorded as {calls}")
        if not math.isclose(submitted[0]["cost"], FOLDCLOUD_PER_CALL) or not math.isclose(
            finished[0]["cost"], gpu_seconds * FOLDCLOUD_PER_GPU_SECOND
        ):
            fail(f"{run['hrid']}'s calls cost {[(c['kind'], c['cost']) for c in calls]}")
        spent = sum(c["cost"] or 0 for c in calls)
        cost = run["cost"] or {}
        if cost.get("currency") != "USD" or not math.isclose(
            cost.get("apis") or 0, spent, abs_tol=1e-4
        ):
            fail(f"{run['hrid']} cost {cost}, not its calls' {spent} USD")
        ok(
            f"{action} called FoldCloud through the egress proxy with the organization's key "
            f"({run['hrid']}): parked on {park['job']}, woke on its webhook after "
            f"{park['polls']} poll(s), made by fold-2 2.1.0, costing {cost['apis']} USD"
        )
        cancel_foldcloud(alice, action, body)
    finally:
        api_call(bob, "DELETE", f"{approvals}/{project['id']}")


def cancel_foldcloud(alice: requests.Session, action: str, body: dict[str, Any]) -> None:
    """Cancel a second job after it parks and check what the provider received."""
    # The fixture spends five seconds on each recycle. A longer job leaves time to
    # observe the park and cancel it through the same API the Runs page uses.
    launched = api_call(
        alice,
        "POST",
        f"/actions/{action}/runs",
        {**body, "params": {"num_recycles": 20}, "consent": [FOLDCLOUD]},
    )
    if launched.status_code != 201:
        fail(f"launching {action} for cancellation answered {launched.status_code}")
    path = f"/runs/{launched.json()['id']}"
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        run = api_call(alice, "GET", path).json()
        if run["status"] == "parked":
            break
        if run["finished_at"]:
            fail(f"{run['hrid']} ended before cancellation: {run['status']}")
        time.sleep(1)
    else:
        fail(f"{path} did not park for cancellation")
    job = run["park"]["job"]
    cancelled = api_call(alice, "POST", f"{path}/cancel", {"reason": "Fixture cancellation check"})
    if cancelled.status_code != 200 or cancelled.json()["status"] != "cancelled":
        fail(f"cancelling {run['hrid']} answered {cancelled.status_code}: {cancelled.text[:300]}")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ended = api_call(alice, "GET", path).json()
        if ended["park"]["outcome"] == "cancelled":
            break
        time.sleep(1)
    else:
        fail(f"{run['hrid']}'s park did not record its cancellation")
    calls = api_call(alice, "GET", f"{path}/api-calls").json()["items"]
    sent = [(c["method"], c["path"], c["status"]) for c in calls if c["kind"] == "cancel"]
    if sent != [("POST", f"/v1/predictions/{job}/cancel", 200)] or ended["effects"]:
        fail(f"{run['hrid']}'s cancellation recorded {sent} and effects {ended['effects']}")
    ok(f"{action} cancelled {job} at FoldCloud without importing a structure")


#: Aspirin, its standard InChIKey, and its sodium salt.
ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"
ASPIRIN_KEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
ASPIRIN_SODIUM = "CC(=O)Oc1ccccc1C(=O)[O-].[Na+]"
#: Caffeine's standard InChIKey, as PubChem gives it for CID 2519.
CAFFEINE_KEY = "RYYVLZVUVIJVGH-UHFFFAOYSA-N"


def compounds(alice: requests.Session, project: dict[str, Any]) -> None:
    """The small_molecule plugin, in its environment with RDKit: aspirin and its sodium
    salt registered as one compound, by its InChIKey, with a batch of each; its
    descriptors; aspirin drawn another way, which is the same compound, reused;
    caffeine fetched from PubChem; and a substructure only aspirin has, found."""
    action = "iki/small_molecule.register_compounds"
    params = {
        "smiles": f"{ASPIRIN} aspirin\n{ASPIRIN_SODIUM} aspirin sodium",
        "parent": True,
        "batches": True,
    }
    run = launch(alice, project, action, {}, params)
    made = [e for e in run["effects"] if e["kind"] in ("created", "reused")]
    found = [e for e in made if e["type"] == "iki/small_molecule.Compound"]
    lots = [e for e in made if e["type"] == "iki/small_molecule.CompoundBatch"]
    if len(found) != 1 or len(lots) != 2 or run["metrics"] != {"lines": 2, "skipped": 0}:
        fail(f"{run['hrid']} ({action}) made {run['effects']}, not one compound and two batches")
    compound = record(alice, found[0]["entity"])
    if (compound["values"]["inchikey"], compound["values"]["smiles"]) != (ASPIRIN_KEY, ASPIRIN):
        fail(f"{run['hrid']} registered aspirin as {compound['values']}")
    salts = sorted(
        (
            values.get("salt") or "",
            values.get("formula_weight"),
            values["compound"],
        )
        for values in (
            api_call(alice, "GET", f"/entities/{lot['entity']}").json()["values"] for lot in lots
        )
    )
    aspirin = entry_of(compound)
    if salts != [("", 180.159, aspirin), ("sodium", 202.141, aspirin)]:
        fail(f"{run['hrid']}'s batches of {compound['hrid']} are {salts}")
    making = api_call(alice, "GET", f"/entities/{compound['id']}/making").json()
    if not any(m["by"] == "iki" and m["software"] == "RDKit" for m in making):
        fail(f"{compound['hrid']}'s making is {making}, not RDKit's, recorded by Iki")
    ok(
        f"{action} registered aspirin and its sodium salt as {compound['hrid']} "
        f"({ASPIRIN_KEY}), with a batch of each, the salt's 202.141 g/mol ({run['hrid']})"
    )
    run = launch(
        alice, project, "iki/small_molecule.compute_descriptors", {"compound": compound["id"]}
    )
    got = api_call(alice, "GET", f"/entities/{compound['id']}").json()["values"]
    expected = {
        "molecular_formula": "C9H8O4",
        "molecular_weight": 180.159,
        "tpsa": 63.6,
        "hbd": 1,
        "hba": 3,
        "lipinski_violations": 0,
    }
    if {k: got.get(k) for k in expected} != expected or not 1.2 < got.get("logp", 0) < 1.4:
        fail(f"{run['hrid']} wrote {got}, not aspirin's descriptors")
    ok(
        f"iki/small_molecule.compute_descriptors ran in RDKit's environment ({run['hrid']}): "
        f"{got['molecular_formula']}, {got['molecular_weight']} g/mol, logP {got['logp']}, "
        f"TPSA {got['tpsa']} Å²"
    )
    run = launch(alice, project, action, {}, {"smiles": "OC(=O)c1ccccc1OC(C)=O"})
    again = [e for e in run["effects"] if e["type"] == "iki/small_molecule.Compound"]
    if [(e["kind"], e["entity"]) for e in again] != [("reused", compound["id"])]:
        fail(f"{run['hrid']} made {run['effects']}, not {compound['hrid']} reused")
    ok(f"aspirin drawn another way is {compound['hrid']}, reused ({run['hrid']})")
    caffeine = pubchem(alice, project)
    substructure(alice, project, compound, caffeine)


#: Nusinersen, by its HELM: 18 2′-MOE nucleotides, every C a 5-methylcytosine, joined by
#: phosphorothioates. PubChem (CID 131801471) gives C234H340N61O128P17S17, 7127 g/mol.
NUSINERSEN = (
    "RNA1{"
    + "[sP].".join(f"[MOE]({'[5meC]' if b == 'C' else b})" for b in "TCACTTTCATAATGCTGG")
    + "}$$$$V2.0"
)


def oligonucleotides(alice: requests.Session, project: dict[str, Any]) -> None:
    """The oligo plugin, in its environment with RDKit: nusinersen registered by its HELM,
    with the library's monomers it's made of, weighing what PubChem says; then written
    another way (an annotation, needless brackets), which is the same entry, reused."""
    action = "iki/oligo.register_oligonucleotide"
    run = launch(alice, project, action, {}, {"helm": NUSINERSEN, "name": "nusinersen"})
    made = [e for e in run["effects"] if e["type"] == "iki/oligo.Oligonucleotide"]
    if len(made) != 1:
        fail(f"{run['hrid']} ({action}) made {run['effects']}, not one oligonucleotide")
    oligo = record(alice, made[0]["entity"])
    got = oligo["values"]
    expected = ("C234H340N61O128P17S17", "TCACTTTCATAATGCTGG", 18)
    weight = got.get("molecular_weight")
    if (got.get("molecular_formula"), got.get("bases"), got.get("length")) != expected or not (
        isinstance(weight, (int, float)) and abs(weight - 7127.17) < 0.01
    ):
        fail(f"{run['hrid']} registered nusinersen as {got}")
    if len(got.get("monomers") or []) != 6:
        fail(f"{oligo['hrid']} lists monomers {got.get('monomers')}, not its six")
    ok(
        f"{action} registered nusinersen by its HELM as {oligo['hrid']}: "
        f"{got['molecular_formula']}, {weight} g/mol, from its six monomers ({run['hrid']})"
    )
    written = NUSINERSEN.replace("RNA1{[MOE](T)", "RNA1{[MOE]([T])").replace(
        "}$$$$V2.0", '}"nusinersen"$$$$V2.0'
    )
    run = launch(alice, project, action, {}, {"helm": written})
    again = [e for e in run["effects"] if e["type"] == "iki/oligo.Oligonucleotide"]
    if [(e["kind"], e["entity"]) for e in again] != [("reused", oligo["id"])]:
        fail(f"{run['hrid']} made {run['effects']}, not {oligo['hrid']} reused")
    ok(f"nusinersen's HELM written another way is {oligo['hrid']}, reused ({run['hrid']})")


def conjugates(alice: requests.Session, project: dict[str, Any]) -> None:
    """Register the adenylated strand in PDB 8V1W and a demonstration lot of it."""
    # PDB 8V1W, chain C: its SEQRES has seven bases, its LINK joins DG1 P to AMP101 O3P.
    # https://files.rcsb.org/view/8V1W.pdb
    helm = "RNA1{P." + "P.".join(f"[dR]({base})" for base in "GTCGGAC") + "}$$$$V2.0"
    carrier_run = launch(alice, project, "iki/oligo.register_oligonucleotide", {}, {"helm": helm})
    (carrier,) = [
        e["entity"] for e in carrier_run["effects"] if e["type"] == "iki/oligo.Oligonucleotide"
    ]
    # Free AMP as deposited in the PDB Chemical Component Dictionary, UDMBCSSLTHHNCD-KQYNXXCUSA-N.
    # https://files.rcsb.org/ligands/view/AMP.cif
    amp_smiles = "Nc1ncnc2n(cnc12)[C@@H]3O[C@H](CO[P](O)(O)=O)[C@@H](O)[C@H]3O"
    amp_run = launch(
        alice, project, "iki/small_molecule.register_compounds", {}, {"smiles": f"{amp_smiles} AMP"}
    )
    (amp,) = [e["entity"] for e in amp_run["effects"] if e["type"] == "iki/small_molecule.Compound"]
    launch(alice, project, "iki/small_molecule.compute_descriptors", {"compound": amp})
    conjugate = created(
        alice,
        project,
        "iki/conjugate.Conjugate",
        {
            "name": "5′-adenylated DNA (PDB 8V1W, chain C)",
            "carrier": [{"kind": "oligo_conjugate", "molecule": entry_of(record(alice, carrier))}],
            "attachments": [
                {
                    "part": entry_of(record(alice, amp)),
                    "site": "5′ phosphate",
                    "copies": 1,
                    "loss_formula": "H2O",
                }
            ],
        },
    )
    run = launch(
        alice, project, "iki/conjugate.compute_composition", {"conjugate": conjugate["id"]}
    )
    values = record(alice, conjugate["id"])["values"]
    # Free-acid DNA C68H87N28O43P7 + AMP C10H14N5O7P less H2O at the diphosphate.
    expected = {
        "molecular_formula": "C78H99N33O49P8",
        "molecular_weight": 2530.62,
        "monoisotopic_mass": 2529.417,
    }
    if any(values.get(k) != v for k, v in expected.items()):
        fail(f"{run['hrid']} computed {values} for the adenylated strand, expected {expected}")
    lot = created(
        alice,
        project,
        "iki/lots.ConjugateLot",
        {
            "conjugate": conjugate["id"],
            "lot_number": f"DEMO-8V1W-{secrets.token_hex(3)}",
            "notes": "Demonstration lot of the deposited structure.",
        },
    )
    if record(alice, lot["id"])["values"]["conjugate"] != entry_of(conjugate):
        fail(f"{lot['hrid']} did not retain its reference to {conjugate['hrid']}")
    ok(f"{lot['hrid']} links to {conjugate['hrid']}, the adenylated strand at 2530.62 u")


def polymers(alice: requests.Session, project: dict[str, Any]) -> None:
    """Register the polystyrene design and the certified measurements of NIST SRM 1478."""
    # Styrene, PubChem CID 7501: https://pubchem.ncbi.nlm.nih.gov/compound/7501
    registered = launch(
        alice,
        project,
        "iki/small_molecule.register_compounds",
        {},
        {"smiles": "C=Cc1ccccc1 styrene"},
    )
    (styrene,) = [
        e["entity"] for e in registered["effects"] if e["type"] == "iki/small_molecule.Compound"
    ]
    precursor = entry_of(record(alice, styrene))
    # A representative uncapped, head-to-tail polystyrene fragment. The certificate
    # reports the bulk sample's averages. It does not identify its end groups.
    formulation = "{[][<]CC(c1ccccc1)[>][]}"
    polymer = created(
        alice,
        project,
        "iki/polymer.Polymer",
        {
            "name": "Polystyrene",
            "bigsmiles": formulation,
            "monomers": [precursor],
            "architecture": "linear",
            "sequence_distribution": "homopolymer",
        },
    )
    if not polymer.get("entry"):
        fail(f"{polymer['hrid']} has no registry entry for the polystyrene design")
    inspection = launch(alice, project, "iki/polymer.inspect_bigsmiles", {"polymer": polymer["id"]})
    design = record(alice, polymer["id"])["values"]
    expected = {
        "bigsmiles": formulation,
        "monomers": [precursor],
        "notation_status": "checked",
        "stochastic_objects": 1,
        "repeat_units": [{"object": 1, "fragment": "[<]CC(c1ccccc1)[>]"}],
    }
    if any(design.get(k) != v for k, v in expected.items()):
        fail(f"{inspection['hrid']} read {polymer['hrid']} as {design}, expected {expected}")
    # https://tsapps.nist.gov/srmext/certificates/1478.pdf, July 1992, certified values.
    lot = created(
        alice,
        project,
        "iki/lots.PolymerLot",
        {
            "polymer": polymer["id"],
            "catalogue_number": "SRM 1478",
            "supplier": "National Institute of Standards and Technology",
            "notes": "Demonstration lot. Certified values from NIST SRM 1478, July 1992.",
            "mn": 35800,
            "mn_method": "membrane_osmometry",
            "mw": 37400,
            "mw_method": "sedimentation_equilibrium",
        },
    )
    run = launch(alice, project, "iki/lots.compute_polymer_lot", {"lot": lot["id"]})
    measured = record(alice, lot["id"])["values"]
    # Molar-mass dispersity, Mw/Mn: https://doi.org/10.1351/PAC-REC-08-05-02
    if (measured.get("polymer"), measured.get("dispersity")) != (entry_of(polymer), 1.045):
        fail(f"{run['hrid']} returned {measured} for NIST SRM 1478")
    ok(f"{lot['hrid']} links to {polymer['hrid']} and has Đ 1.045 from the SRM 1478 certificate")


#: Cloning vector pUC19, 2,686 bp, as INSDC record L09137.2 gives it: the vector most
#: molecular cloning starts from (Yanisch-Perron, Vieira & Messing, Gene 33:103, 1985).
PUC19 = (
    "TCGCGCGTTTCGGTGATGACGGTGAAAACCTCTGACACATGCAGCTCCCGGAGACGGTCACAGCTTGTCTGT"
    "AAGCGGATGCCGGGAGCAGACAAGCCCGTCAGGGCGCGTCAGCGGGTGTTGGCGGGTGTCGGGGCTGGCTTA"
    "ACTATGCGGCATCAGAGCAGATTGTACTGAGAGTGCACCATATGCGGTGTGAAATACCGCACAGATGCGTAA"
    "GGAGAAAATACCGCATCAGGCGCCATTCGCCATTCAGGCTGCGCAACTGTTGGGAAGGGCGATCGGTGCGGG"
    "CCTCTTCGCTATTACGCCAGCTGGCGAAAGGGGGATGTGCTGCAAGGCGATTAAGTTGGGTAACGCCAGGGT"
    "TTTCCCAGTCACGACGTTGTAAAACGACGGCCAGTGAATTCGAGCTCGGTACCCGGGGATCCTCTAGAGTCG"
    "ACCTGCAGGCATGCAAGCTTGGCGTAATCATGGTCATAGCTGTTTCCTGTGTGAAATTGTTATCCGCTCACA"
    "ATTCCACACAACATACGAGCCGGAAGCATAAAGTGTAAAGCCTGGGGTGCCTAATGAGTGAGCTAACTCACA"
    "TTAATTGCGTTGCGCTCACTGCCCGCTTTCCAGTCGGGAAACCTGTCGTGCCAGCTGCATTAATGAATCGGC"
    "CAACGCGCGGGGAGAGGCGGTTTGCGTATTGGGCGCTCTTCCGCTTCCTCGCTCACTGACTCGCTGCGCTCG"
    "GTCGTTCGGCTGCGGCGAGCGGTATCAGCTCACTCAAAGGCGGTAATACGGTTATCCACAGAATCAGGGGAT"
    "AACGCAGGAAAGAACATGTGAGCAAAAGGCCAGCAAAAGGCCAGGAACCGTAAAAAGGCCGCGTTGCTGGCG"
    "TTTTTCCATAGGCTCCGCCCCCCTGACGAGCATCACAAAAATCGACGCTCAAGTCAGAGGTGGCGAAACCCG"
    "ACAGGACTATAAAGATACCAGGCGTTTCCCCCTGGAAGCTCCCTCGTGCGCTCTCCTGTTCCGACCCTGCCG"
    "CTTACCGGATACCTGTCCGCCTTTCTCCCTTCGGGAAGCGTGGCGCTTTCTCATAGCTCACGCTGTAGGTAT"
    "CTCAGTTCGGTGTAGGTCGTTCGCTCCAAGCTGGGCTGTGTGCACGAACCCCCCGTTCAGCCCGACCGCTGC"
    "GCCTTATCCGGTAACTATCGTCTTGAGTCCAACCCGGTAAGACACGACTTATCGCCACTGGCAGCAGCCACT"
    "GGTAACAGGATTAGCAGAGCGAGGTATGTAGGCGGTGCTACAGAGTTCTTGAAGTGGTGGCCTAACTACGGC"
    "TACACTAGAAGAACAGTATTTGGTATCTGCGCTCTGCTGAAGCCAGTTACCTTCGGAAAAAGAGTTGGTAGC"
    "TCTTGATCCGGCAAACAAACCACCGCTGGTAGCGGTGGTTTTTTTGTTTGCAAGCAGCAGATTACGCGCAGA"
    "AAAAAAGGATCTCAAGAAGATCCTTTGATCTTTTCTACGGGGTCTGACGCTCAGTGGAACGAAAACTCACGT"
    "TAAGGGATTTTGGTCATGAGATTATCAAAAAGGATCTTCACCTAGATCCTTTTAAATTAAAAATGAAGTTTT"
    "AAATCAATCTAAAGTATATATGAGTAAACTTGGTCTGACAGTTACCAATGCTTAATCAGTGAGGCACCTATC"
    "TCAGCGATCTGTCTATTTCGTTCATCCATAGTTGCCTGACTCCCCGTCGTGTAGATAACTACGATACGGGAG"
    "GGCTTACCATCTGGCCCCAGTGCTGCAATGATACCGCGAGACCCACGCTCACCGGCTCCAGATTTATCAGCA"
    "ATAAACCAGCCAGCCGGAAGGGCCGAGCGCAGAAGTGGTCCTGCAACTTTATCCGCCTCCATCCAGTCTATT"
    "AATTGTTGCCGGGAAGCTAGAGTAAGTAGTTCGCCAGTTAATAGTTTGCGCAACGTTGTTGCCATTGCTACA"
    "GGCATCGTGGTGTCACGCTCGTCGTTTGGTATGGCTTCATTCAGCTCCGGTTCCCAACGATCAAGGCGAGTT"
    "ACATGATCCCCCATGTTGTGCAAAAAAGCGGTTAGCTCCTTCGGTCCTCCGATCGTTGTCAGAAGTAAGTTG"
    "GCCGCAGTGTTATCACTCATGGTTATGGCAGCACTGCATAATTCTCTTACTGTCATGCCATCCGTAAGATGC"
    "TTTTCTGTGACTGGTGAGTACTCAACCAAGTCATTCTGAGAATAGTGTATGCGGCGACCGAGTTGCTCTTGC"
    "CCGGCGTCAATACGGGATAATACCGCGCCACATAGCAGAACTTTAAAAGTGCTCATCATTGGAAAACGTTCT"
    "TCGGGGCGAAAACTCTCAAGGATCTTACCGCTGTTGAGATCCAGTTCGATGTAACCCACTCGTGCACCCAAC"
    "TGATCTTCAGCATCTTTTACTTTCACCAGCGTTTCTGGGTGAGCAAAAACAGGAAGGCAAAATGCCGCAAAA"
    "AAGGGAATAAGGGCGACACGGAAATGTTGAATACTCATACTCTTCCTTTTTCAATATTATTGAAGCATTTAT"
    "CAGGGTTATTGTCTCATGAGCGGATACATATTTGAATGTATTTAGAAAAATAAACAAATAGGGGTTCCGCGC"
    "ACATTTCCCCGAAAAGTGCCACCTGACGTCTAAGAAACCATTATTATCATGACATTAACCTATAAAAATAGG"
    "CGTATCACGAGGCCCTTTCGTC"
)

#: The stretch of pUC19 between its EcoRI and HindIII sites, with a few bases either
#: side, as a PCR product of its polylinker would be: 66 bp of its own real sequence.
POLYLINKER = PUC19[389:455]


def give_lab_roles(alice: requests.Session, project: dict[str, Any], *roles: str) -> None:
    members = api_call(alice, "GET", f"/projects/{project['id']}/members?limit=100").json()
    grant = next(m for m in members["items"] if (m["user"] or {}).get("email") == "alice@iki.test")
    held = {r["role"] for r in grant["lab_roles"]}
    given = api_call(
        alice,
        "PUT",
        f"/projects/{project['id']}/members/{grant['id']}/lab-roles",
        {"roles": sorted(held.union(roles))},
    )
    if given.status_code != 200:
        fail(f"giving alice {', '.join(roles)} answered HTTP {given.status_code}")


def matching_records(
    session: requests.Session, project: dict[str, Any], kind: str, field: str, value: str
) -> list[dict[str, Any]]:
    query = {
        "project": project["id"],
        "type": kind,
        "where": {"field": field, "op": "eq", "value": value},
        "limit": 100,
    }
    response = api_call(session, "POST", "/entities/search", query)
    if response.status_code != 200:
        fail(f"finding {kind} by {field} answered HTTP {response.status_code}: {response.text}")
    found: list[dict[str, Any]] = response.json()["items"]
    return found


def plate_assay(alice: requests.Session, project: dict[str, Any]) -> None:
    """Import the public Tox21 matrix and sign the research fits."""
    prefix = "iki/plate_assays."
    give_lab_roles(alice, project, prefix + "analyst", prefix + "reviewer")
    compounds = {
        key: created(
            alice, project, "iki/small_molecule.Compound", public_plate.compound_values(key)
        )["id"]
        for key in public_plate.SERIES
    }
    name = public_plate.PLATE_NAME
    plates = matching_records(alice, project, prefix + "AssayPlate", "name", name)
    if plates:
        plate = record(alice, plates[0]["id"])
    else:
        fixture = public_plate.READER_FILE
        source = upload(alice, project, fixture.name, fixture.read_bytes(), "text/csv")
        plate = created(
            alice,
            project,
            prefix + "AssayPlate",
            {
                "name": name,
                "size": "plate_96",
                "assignments": public_plate.assignments(compounds),
                "reader_file": source,
                "procedure": public_plate.ASSAY,
                "procedure_revision": public_plate.SOURCE["procedure_revision"],
            },
        )
    fits = fit_assay_plate(alice, project, plate, compounds)
    for entity in fits.values():
        if entity["values"]["review"] == "recorded":
            signature = signing(alice, entity, "signed", "alice", field="review")
            if signature["status"] != "signed" or not signature["set"]:
                fail(f"signing the plate result {entity['hrid']} gave {signature}")
        proofs = api_call(alice, "GET", f"/entities/{entity['id']}/signatures?verify=true").json()
        if len(proofs["items"]) != 1 or not proofs["items"][0]["verification"]["valid"]:
            fail(f"{entity['hrid']} has no verifiable research sign-off")
    ok(f"{plate['hrid']}: 72 measured wells and two signed flavonoid IC50s")
    linked_experiment(
        alice,
        project,
        compounds["apigenin"],
        plate["values"]["reader_file"],
        fits["apigenin"]["id"],
    )
    equipment_calibration(alice, project, fits["apigenin"])


def fit_assay_plate(
    alice: requests.Session,
    project: dict[str, Any],
    plate: dict[str, Any],
    compounds: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Check imported controls, each compound's measurements, and its source potencies."""
    prefix = "iki/plate_assays."
    plate_input = {"plate": plate["id"]}
    if not plate["values"].get("committed"):
        launch(alice, project, prefix + "preview_reader", plate_input)
        preview = record(alice, plate["id"])["values"]["preview"]
        if preview["issues"] or len(preview["readings"]) != 72 or preview["unit"] != "%":
            fail(f"{plate['hrid']} has an unexpected normalized activity preview: {preview}")
        launch(alice, project, prefix + "commit_reader", plate_input)
    [read] = matching_records(alice, project, prefix + "PlateRead", "plate", plate["id"])
    measurements = matching_records(
        alice, project, prefix + "WellMeasurement", "plate", plate["id"]
    )
    measured = {m["values"]["well"]: m for m in measurements}
    if (
        len(measured) != 72
        or set(read["values"]["measurements"]) != {m["id"] for m in measurements}
        or any(
            m["values"]["reader_file"] != plate["values"]["reader_file"]
            or m["values"]["activity_percent"] is None
            or m["values"]["signal"] is not None
            for m in measurements
        )
        or any(
            measured[c["well"]]["values"]["control"] != c["control"]
            for c in public_plate.SOURCE["controls"]
        )
    ):
        fail(f"{read['hrid']} has inconsistent source measurements or controls")

    def sample_results(entry: str) -> list[dict[str, Any]]:
        return [
            r
            for r in matching_records(alice, project, prefix + "AssayResult", "read", read["id"])
            if r["values"]["sample"] == entry
        ]

    fits = {}
    for key, series in public_plate.SERIES.items():
        sample = record(alice, compounds[key])
        results = sample_results(entry_of(sample))
        if not results:
            launch(
                alice,
                project,
                prefix + "fit_curve",
                {"read": read["id"], "sample": sample["id"]},
                {"endpoint_kind": "IC50"},
            )
            results = sample_results(entry_of(sample))
        [result] = results
        entity = record(alice, result["id"])
        v = entity["values"]
        reference = sum(series["potencies_uM"]) / len(series["potencies_uM"])
        if (
            v["point_count"] != 33
            or v["concentration_unit"] != "uM"
            or v["response_unit"] != "percent"
            or v["qualifier"] != "within_range"
            or v["endpoint_kind"] != "IC50"
            or v["procedure"] != public_plate.ASSAY
            or set(v["measurements"])
            != {measured[f"{row}{col}"]["id"] for row in series["rows"] for col in range(1, 12)}
            or not math.isclose(v["endpoint_value"], reference, rel_tol=0.05)
            or not v["ci_low"] <= reference <= v["ci_high"]
        ):
            fail(f"{entity['hrid']} does not match the deposited {series['name']} series: {v}")
        fits[key] = entity
    return fits


class TutorialApi:
    """Run the same tutorial as a lab, through Alice's browser session and upload URLs."""

    def __init__(self, session: requests.Session) -> None:
        self.session = session

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:  # noqa: ANN401 - parsed API response
        response = api_call(self.session, method, path, json, headers)
        if not 200 <= response.status_code < 300:
            fail(f"{method} {path} answered HTTP {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else None

    def upload(self, project: str, path: Path) -> str:
        return upload(self.session, {"id": project}, path.name, path.read_bytes(), "text/csv")


def dose_response_tutorial(alice: requests.Session, project: dict[str, Any]) -> None:
    """Run the Tox21 tutorial in each review flow and return the signed fits to its requester."""
    bob, carol = sign_in_as("bob"), sign_in_as("carol")
    people = {
        name: api_call(s, "GET", "/me").json()["id"]
        for name, s in (("alice", alice), ("bob", bob), ("carol", carol))
    }
    api = TutorialApi(alice)
    team = project["team"]["id"]
    for flow in ("research", "regulated_qc"):
        example = tutorial.prepare(
            api,
            team=team,
            flow=flow,
            state=tutorial.state_path(IKI, team, flow),
            analyst=people["alice"],
            reviewer=people["bob"],
            qa=people["carol"] if flow == "regulated_qc" else None,
        )
        core = api.request("GET", f"/projects/{example['project']}")
        requester = cli_read_token(alice, project=example["requester_project"])
        receive_tutorial_tubes(alice, example["request"], [example[k] for k in public_plate.SERIES])
        plate = record(alice, example["plate"])
        if plate["values"]["reagent_tube"] != example["tube"]:
            fail("the example plate lost its luciferase reagent tube")
        if bearer_call(requester, "GET", f"/entities/{example['tube']}").status_code != 404:
            fail("the requesting project could open the assay group's reagent tube")
        fits = fit_assay_plate(alice, core, plate, {k: example[k] for k in public_plate.SERIES})
        for result in fits.values():
            if (
                result["values"]["review_flow"] != flow
                or result["values"]["request"] != example["request"]
                or plate["values"]["reader_file"] != example["reader_file"]
            ):
                fail(f"{result['hrid']} failed to retain the starter flow, source or request")
            sign_tutorial_result(result, example["request"], requester, alice, bob, carol, people)
        ok(f"{core['name']} returned both signed flavonoid IC50s to its tutorial requester")


def sign_tutorial_result(
    result: dict[str, Any],
    request: str,
    requester: str,
    alice: requests.Session,
    bob: requests.Session,
    carol: requests.Session,
    people: dict[str, str],
) -> None:
    flow = result["values"]["review_flow"]
    steps = (
        [("signed", bob, "bob")]
        if flow == "research"
        else [
            ("submitted", alice, "alice"),
            ("reviewed", bob, "bob"),
            ("released", carol, "carol"),
        ]
    )
    result_path = f"/entities/{result['id']}"
    states = ["recorded", *(target for target, _, _ in steps)]
    # The seed may have been interrupted after a signature, or completed on a previous run.
    for target, session, signer in steps[states.index(result["values"]["review"]) :]:
        if bearer_call(requester, "GET", result_path).status_code != 404:
            fail(f"the requester could see {result['hrid']} before its final signature")
        signed = signing(session, result, target, signer, field="review")
        if signed["status"] != "signed" or not signed["set"]:
            fail(f"signing {result['hrid']} as {target} gave {signed}")
    delivered = bearer_call(requester, "GET", result_path).json()
    proofs = bearer_call(requester, "GET", f"{result_path}/signatures?verify=true").json()["items"]
    receipts = bearer_call(requester, "GET", f"/entities/{request}/request").json()[
        "result_releases"
    ]
    expected = {target: people[user] for target, _, user in steps}
    if (
        delivered["values"]["review"] != steps[-1][0]
        or delivered["values"]["points"] != result["values"]["points"]
        or not all(p["verification"]["valid"] for p in proofs)
        or len(proofs) != len(steps)
        or {p["change"]["to"]: p["signer"]["id"] for p in proofs} != expected
        or [(r["entity"], r["released_revision"]) for r in receipts if r["entity"] == result["id"]]
        != [(result["id"], delivered["revision"])]
    ):
        fail(f"{result['hrid']} did not return the signed fit and its release revision")


def receive_tutorial_tubes(alice: requests.Session, request_id: str, tube_ids: list[str]) -> None:
    """Scan all the labelled tubes once, even if the dev check is being run again."""
    path = f"/entities/{request_id}/receipts"
    prior = api_call(alice, "GET", path).json()["receipts"]
    completed = [r for r in prior if r["status"] == "completed"]
    if not completed:
        started = api_call(alice, "POST", path)
        if started.status_code != 201:
            fail(f"starting the example receipt answered {started.status_code}: {started.text}")
        receipt = started.json()
        at = f"/receipts/{receipt['id']}"
        lines = []
        revision = receipt["plan_revision"]
        for tube_id in tube_ids:
            tube = record(alice, tube_id)
            code = tube["values"].get("barcode") or tube["hrid"]
            scanned = api_call(alice, "POST", f"{at}/scan", {"code": code})
            if scanned.status_code != 200 or scanned.json()["found"] not in ("matched", "again"):
                fail(f"scanning {tube['hrid']} answered {scanned.status_code}: {scanned.text}")
            lines.append(scanned.json()["line"])
            revision = scanned.json()["receipt"]["plan_revision"]
        custody = api_call(
            alice,
            "PATCH",
            at,
            {
                "handed_over_by": "Example courier, training run",
                "plan_revision": revision,
            },
        )
        if custody.status_code != 200:
            fail(f"recording the example handover answered {custody.status_code}: {custody.text}")
        done = api_call(
            alice,
            "POST",
            f"{at}/complete",
            {
                "plan_revision": custody.json()["plan_revision"],
                "keep_in_custody": lines,
            },
        )
        if done.status_code != 200:
            fail(f"completing the example receipt answered {done.status_code}: {done.text}")
        completed = [done.json()]
    if len(completed) != 1:
        fail(f"the tutorial request has {len(completed)} completed receipts")
    received = read_completed_receipt(alice, completed[0]["id"])
    if {
        line["entity"]
        for line in received["lines"]
        if line["outcome"] == "matched" and line["received_unplaced"] and line["put_away"] is None
    } != set(tube_ids) or len(received["lines"]) != len(tube_ids):
        fail(f"the tutorial tubes were not received in unplaced custody: {received['lines']}")


def linked_experiment(
    alice: requests.Session, project: dict[str, Any], sample: str, raw_file: str, result: str
) -> None:
    """Witness an ELN entry revision, its material movement and the signed plate result."""
    prefix = "iki/experiments."
    give_lab_roles(alice, project, prefix + "author", "iki/lots.qa")
    title = "Firefly luciferase inhibition in the ELN"
    records = matching_records(alice, project, prefix + "Experiment", "title", title)
    experiment = (
        records[0]
        if records
        else created(
            alice,
            project,
            prefix + "Experiment",
            {
                "title": title,
                "eln_id": "TOX21-FLUC-01",
                "eln_url": "https://eln.example.org/entries/TOX21-FLUC-01",
                "eln_revision": "2",
                "protocol_id": public_plate.ASSAY,
                "protocol_revision": public_plate.SOURCE["procedure_revision"],
                "samples": [sample],
                "observations": [
                    {
                        "observed_at": "2025-01-01T00:00:00Z",
                        "note": "Tox21 apigenin activity against firefly luciferase.",
                    }
                ],
                "attachments": [raw_file],
            },
        )
    )
    # A bought reagent lot can be released by the person recording its receipt (ADR 0162).
    lot_number = "EXAMPLE-APIGENIN-01"
    lots = matching_records(alice, project, "iki/lots.GenericLot", "lot_number", lot_number)
    if lots:
        lot = lots[0]
    else:
        certificate = upload(
            alice,
            project,
            "synthetic-certificate.csv",
            b"lot,certificate\nEXAMPLE-APIGENIN-01,synthetic example\n",
            "text/csv",
        )
        lot = created(
            alice,
            project,
            "iki/lots.GenericLot",
            {
                "of": sample,
                "lot_number": lot_number,
                "source": "bought",
                "certificates": [certificate],
            },
            {"value": "10", "unit": "mg"},
        )
    if lot["values"]["disposition"] == "quarantined":
        released = signing(alice, lot, "released", "alice", field="disposition")
        if not released["set"]:
            fail(f"QA release of the synthetic reagent failed: {released}")
    lot_path = f"/entities/{lot['id']}/amount"
    ledger = api_call(alice, "GET", lot_path + "/movements").json()["items"]
    used = [movement for movement in ledger if movement["used_in"] == experiment["id"]]
    if not used:
        drawn = api_call(
            alice,
            "POST",
            lot_path + "/draw",
            {
                "amount": {"value": "1.5", "unit": "mg"},
                "used_in": experiment["id"],
                "reason": "Synthetic standard for the MARS run",
            },
        )
        if drawn.status_code != 200 or drawn.json()["amount"]["value"] != "8.5":
            fail(
                f"drawing into {experiment['hrid']} answered HTTP {drawn.status_code}: {drawn.text}"
            )
        used = [drawn.json()["movement"]]
    [movement] = used
    experiment = record(alice, experiment["id"])
    if not experiment["values"].get("result_pins"):
        launch(
            alice,
            project,
            prefix + "capture_results",
            {"experiment": experiment["id"], "results": [result]},
        )
    witness = sign_in_as("bob")
    person = assign_reviewer(alice, witness, project, prefix + "reviewer")
    experiment = record(witness, experiment["id"])
    if experiment["values"]["review"] == "recorded":
        signed = signing(witness, experiment, "witnessed", "bob", field="review")
        if not signed["set"]:
            fail(f"witnessing {experiment['hrid']} failed: {signed}")
    path = f"/entities/{experiment['id']}"
    [proof] = api_call(witness, "GET", path + "/signatures?verify=true").json()["items"]
    snapshot = api_call(witness, "GET", f"{path}/revisions/{proof['revision']}").json()
    values = snapshot["values"]
    [pin] = values["result_pins"]
    [draw] = values["material_draws"]
    pinned = api_call(
        witness, "GET", f"/entities/{result}/revisions/{pin['record_revision']}"
    ).json()
    if (
        not proof["verification"]["valid"]
        or proof["meaning"] != "witness"
        or proof["signer"]["id"] != person
        or pin["result"] != result
        or pin["record_id"] != result
        or pinned["values"]["review"] != "signed"
        or draw["movement"] != movement["id"]
        or draw["record"] != lot["id"]
        or draw["unit"] != "mg"
        or Decimal(draw["amount"]) != Decimal("1.5")
        or values["eln_revision"] != "2"
        or values["protocol_id"] != public_plate.ASSAY
        or values["protocol_revision"] != public_plate.SOURCE["procedure_revision"]
        or values["samples"] != [entry_of(record(witness, sample))]
        or raw_file not in snapshot["files"]
    ):
        fail(f"{experiment['hrid']} did not witness the ELN, protocol, plate and lot evidence")
    ok(f"{experiment['hrid']}: witnessed the ELN and result revisions with the 1.5 mg draw")


def reagent_stock(alice: requests.Session, project: dict[str, Any]) -> None:
    """Receive two labelled racks, release their lots and draw three tubes at the bench."""
    prefix = "iki/reagents."
    give_lab_roles(alice, project, prefix + "stock_manager", "iki/lots.qa")
    stock = f"/projects/{project['id']}/stock"

    def rows(path: str) -> list[dict[str, Any]]:
        response = api_call(alice, "GET", path + "?limit=100")
        if response.status_code != 200:
            fail(f"reading {path} answered HTTP {response.status_code}: {response.text}")
        found: list[dict[str, Any]] = response.json()["items"]
        return found

    def ensure(kind: str, field: str, values: dict[str, Any]) -> dict[str, Any]:
        found = matching_records(alice, project, prefix + kind, field, values[field])
        return found[0] if found else created(alice, project, prefix + kind, values)

    # Corning's catalogue no. 430790 specifies 50 tubes per rack and 500 per case.
    # https://ecatalog.corning.com/life-sciences/b2b/us/en/Liquid-Handling/Tubes%2C-Liquid-Handling/Centrifuge-Tubes/Corning%C2%AE-15mL-Centrifuge-Tubes/p/430790
    item = ensure(
        "StockItem",
        "catalogue_number",
        {
            "name": "Corning 15 mL centrifuge tubes",
            "supplier": "Corning",
            "catalogue_number": "430790",
            "pack_size": 50,
            "stock_unit": "1",
            "low_stock_at": 98,
        },
    )
    order_reference = "SYNTH-430790-ORDER-01"
    orders = matching_records(
        alice, project, prefix + "ReorderLine", "order_reference", order_reference
    )
    if not orders:
        launch(
            alice,
            project,
            prefix + "place_order",
            {"item": item["id"]},
            {"order_reference": order_reference, "ordered_packs": 2},
        )
        orders = matching_records(
            alice, project, prefix + "ReorderLine", "order_reference", order_reference
        )
    [order] = orders
    lot_ids = []
    location_ids = []
    certificate = None
    for line in (1, 2):
        location = ensure("StockLocation", "name", {"name": f"Synthetic tube storage {line}"})
        location_ids.append(location["id"])
        number = f"SYNTH-430790-LOT-{line}"
        lots = matching_records(alice, project, prefix + "CountStockLot", "lot_number", number)
        if not lots:
            if certificate is None:
                certificate = upload(
                    alice,
                    project,
                    "synthetic-tube-certificate.csv",
                    b"lot,certificate\nSYNTH-430790-LOT-1,synthetic\nSYNTH-430790-LOT-2,synthetic\n",
                    "text/csv",
                )
            launch(
                alice,
                project,
                prefix + "receive_delivery",
                {"order": order["id"], "storage": location["id"]},
                {
                    "packs": 1,
                    "delivery_note": f"SYNTH-DELIVERY-{line}",
                    "delivery_line": "1",
                    "lot_number": number,
                    "received_on": f"2025-01-0{line}",
                    "expires_on": "2099-01-01",
                    "certificate": certificate,
                },
            )
            lots = matching_records(alice, project, prefix + "CountStockLot", "lot_number", number)
        [lot] = lots
        lot_ids.append(lot["id"])
        ordered = record(alice, order["id"])["values"]
        if line == 1 and ordered["received_packs"] == 1:
            [outstanding] = [row for row in rows(stock + "/orders") if row["id"] == order["id"]]
            if outstanding["outstanding_packs"] != 1:
                fail(f"{order['hrid']} should have one rack left to receive")
        if lot["values"]["disposition"] == "quarantined":
            released = signing(alice, lot, "released", "alice", field="disposition")
            if not released["set"]:
                fail(f"QA release of {lot['hrid']} failed: {released}")
    if record(alice, order["id"])["values"]["received_packs"] != 2 or any(
        row["id"] == order["id"] for row in rows(stock + "/orders")
    ):
        fail(f"{order['hrid']} still appears outstanding after both racks arrived")
    title = "Synthetic stock-use experiment"
    kind = "iki/experiments.Experiment"
    experiments = matching_records(alice, project, kind, "title", title)
    experiment = (
        experiments[0]
        if experiments
        else created(
            alice,
            project,
            kind,
            {
                "title": title,
                "eln_id": "SYNTH-STOCK-USE-01",
                "eln_revision": "1",
                "protocol_id": "SYNTH-TUBE-USE",
                "protocol_revision": "1",
            },
        )
    )
    lot_path = f"/entities/{lot_ids[0]}/amount"
    movements = api_call(alice, "GET", lot_path + "/movements").json()["items"]
    used = [move for move in movements if move["used_in"] == experiment["id"]]
    if not used:
        [before] = [row for row in rows(stock) if row["id"] == item["id"]]
        if Decimal(before["available"]) != 100 or before["low"]:
            fail(f"two released racks should provide 100 tubes: {before}")
        drawn = api_call(
            alice,
            "POST",
            lot_path + "/draw",
            {
                "amount": {"value": 3, "unit": "1"},
                "used_in": experiment["id"],
                "reason": "Synthetic use of three tubes at the bench",
            },
        )
        if drawn.status_code != 200 or Decimal(drawn.json()["amount"]["value"]) != 47:
            fail(f"drawing three tubes answered HTTP {drawn.status_code}: {drawn.text}")
        used = [drawn.json()["movement"]]
    [movement] = used
    experiment_values = record(alice, experiment["id"])["values"]
    [receipt] = experiment_values["material_draws"]
    if (
        experiment_values["review"] != "recorded"
        or experiment_values["eln_id"] != "SYNTH-STOCK-USE-01"
        or experiment_values["eln_revision"] != "1"
        or movement["used_in"] != experiment["id"]
        or movement["kind"] != "drawn"
        or movement["unit"] != "1"
        or Decimal(movement["change"]) != -3
        or receipt["movement"] != movement["id"]
        or receipt["record"] != lot_ids[0]
        or receipt["unit"] != "1"
        or Decimal(receipt["amount"]) != 3
    ):
        fail(f"{experiment['hrid']} should record the three-tube withdrawal: {receipt}")
    [now] = [row for row in rows(stock) if row["id"] == item["id"]]
    locations = {
        row["container_id"]: (row["id"], Decimal(row["amount"]))
        for row in rows(f"{stock}/{item['id']}/lots")
    }
    if (
        not now["low"]
        or any(Decimal(now[field]) != 97 for field in ("physical", "stored", "available"))
        or locations
        != {location_ids[0]: (lot_ids[0], Decimal(47)), location_ids[1]: (lot_ids[1], Decimal(50))}
    ):
        fail(f"{item['hrid']} should be low with 47 and 50 tubes stored: {now}, {locations}")
    # Run the hourly maintenance command now to exercise the low-stock notification.
    run_maintenance()
    notices = api_call(alice, "GET", "/notifications?limit=100").json()["items"]
    if not any(
        note["kind"] == "stock.low" and any(r["id"] == item["id"] for r in note["records"])
        for note in notices
    ):
        fail(f"the stock manager was not notified that {item['hrid']} was low")
    ok(f"{order['hrid']}: both racks arrived and the draw raised a low-stock alert at 97 tubes")


def sequence_library_annotation(alice: requests.Session, project: dict[str, Any]) -> None:
    """Find routine vector features on a DNA sequence and retain its GenBank features."""
    # INSDC OL989372.1, the published circular vector pET-28a-ZaTdT12. Import the
    # pinned GenBank record so the original feature table is present before the run.
    accession = "OL989372.1"
    fixture = Path(__file__).resolve().parents[2] / "server/tests/data/plasmids" / f"{accession}.gb"
    source = upload(alice, project, fixture.name, fixture.read_bytes(), "chemical/seq-na-genbank")
    imported = launch(alice, project, "iki/dna.import_sequences", {}, {"file": source})
    sequences = [e for e in imported["effects"] if e["type"] == "iki/dna.DNASequence"]
    if len(sequences) != 1:
        fail(f"{imported['hrid']} did not import one DNA sequence from {accession}")
    entity = sequences[0]["entity"]
    before = record(alice, entity)["values"]
    if not before.get("features") or before.get("topology") != "circular":
        fail(f"{accession} did not retain its original features and circular topology on import")

    action = "iki/plasmid.annotate_dna_sequence"
    run = launch(alice, project, action, {"sequence": entity})
    dna = record(alice, entity)
    values = dna["values"]
    if values.get("features") != before["features"] or values.get("bases") != before["bases"]:
        fail(f"{run['hrid']} changed {dna['hrid']}'s imported features or bases")
    # The GenBank FEATURES table independently gives these locations on opposite strands.
    expected = {("T7 promoter", 4984, 5002, "forward"), ("lacI promoter", 4598, 4675, "reverse")}
    found = {
        (f["name"], f["start"], f["end"], f["strand"]) for f in values.get("library_features") or []
    }
    if not expected.issubset(found) or not values.get("library_annotated_with"):
        fail(f"{run['hrid']} found {sorted(found)} on {dna['hrid']}, missing matches or provenance")
    ok(f"{action} annotated {dna['hrid']} on both strands and kept its GenBank features")


def equipment_calibration(
    alice: requests.Session, project: dict[str, Any], measured: dict[str, Any]
) -> None:
    """Trace a synthetic calibration failure to an already signed measurement."""
    prefix = "iki/equipment."
    give_lab_roles(alice, project, "iki/lab_roles.engineer", prefix + "qa")
    engineer = api_call(alice, "GET", "/me").json()["id"]
    result = record(alice, measured["id"])
    instrument = created(
        alice, project, "iki/equipment.Instrument", {"name": "Example luminescence plate reader"}
    )
    now = datetime.now(UTC).replace(microsecond=0)
    performed = now - timedelta(days=3)
    measured_at = now - timedelta(days=2)
    failed_at = now - timedelta(days=1)

    def change(
        entity: dict[str, Any], path: str, values: dict[str, Any], *, method: str = "PATCH"
    ) -> None:
        at = f"/entities/{entity['id']}"
        etag = api_call(alice, "GET", at).headers["ETag"]
        changed = api_call(alice, method, at + path, values, {"If-Match": etag})
        if changed.status_code != 200:
            fail(f"changing {entity['hrid']} answered HTTP {changed.status_code}: {changed.text}")

    def appointment(at: datetime, outcome: str, more: dict[str, Any]) -> dict[str, Any]:
        run = launch(
            alice,
            project,
            prefix + "schedule_service",
            {"instrument": instrument["id"]},
            {
                "kind": "calibration",
                "due_on": at.date().isoformat(),
                "procedure": "Synthetic optical check, demonstration revision 1",
            },
        )
        [made] = [effect["entity"] for effect in run["effects"]]
        service = record(alice, made)
        report = upload(
            alice,
            project,
            "synthetic-calibration-report.txt",
            f"Synthetic service report for {instrument['hrid']}: {outcome}.\n".encode(),
            "text/plain",
        )
        change(
            service,
            "",
            {
                "values": {
                    "outcome": outcome,
                    "performed_by": engineer,
                    "performed_at": at.isoformat(),
                    "report": report,
                    **more,
                }
            },
        )
        signed = signing(alice, service, "signed_off", "alice", field="review")
        if signed["status"] != "signed" or not signed["set"]:
            fail(f"signing the equipment service {service['hrid']} answered {signed}")
        return record(alice, made)

    passing = appointment(
        performed, "in_tolerance", {"valid_until": (now + timedelta(days=10)).isoformat()}
    )
    change(instrument, "/states/calibration", {"state": "calibrated", "reason": ""}, method="POST")
    used = launch(
        alice,
        project,
        prefix + "record_instrument_use",
        {"instrument": instrument["id"], "result": result["id"], "calibration": passing["id"]},
        {"measurement_at": measured_at.isoformat()},
    )
    [use_id] = [effect["entity"] for effect in used["effects"]]
    use = record(alice, use_id)
    if (
        use["values"]["calibration_revision"] != passing["revision"]
        or use["values"]["result_revision"] != result["revision"]
    ):
        fail(f"{use['hrid']} did not pin the measurement and calibration revisions")

    failure = appointment(
        failed_at,
        "out_of_tolerance",
        {
            "notes": "Synthetic failure. The passing check was the last known good evidence.",
            "suspect_since": performed.isoformat(),
        },
    )
    for field, state in (("service", "out_of_service"), ("calibration", "failed")):
        change(
            instrument,
            f"/states/{field}",
            {"state": state, "reason": "Synthetic calibration failure"},
            method="POST",
        )
    traced = launch(
        alice,
        project,
        prefix + "trace_failed_calibrations",
        {
            "instrument": instrument["id"],
            "calibrations": [passing["id"], failure["id"]],
            "uses": [use["id"]],
        },
    )
    [case] = [effect["entity"] for effect in traced["effects"]]
    impact = record(alice, case)
    if (
        impact["values"]["use"] != use["id"]
        or impact["values"]["failure_revision"] != failure["revision"]
    ):
        fail(f"calibration impact {impact['hrid']} does not identify the failure and use")
    change(
        impact,
        "",
        {
            "values": {
                "disposition": "repeat_required",
                "reasoning": "Synthetic case; repeat the read.",
            }
        },
    )
    disposition = signing(alice, impact, "signed_off", "alice", field="review")
    original = record(alice, result["id"])
    proofs = api_call(alice, "GET", f"/entities/{result['id']}/signatures?verify=true").json()
    if (
        disposition["status"] != "signed"
        or not disposition["set"]
        or original["revision"] != result["revision"]
        or not proofs["items"][0]["verification"]["valid"]
    ):
        fail(f"{impact['hrid']} did not preserve {result['hrid']}'s signed revision")
    ok(f"{use['hrid']} pins the calibration; {impact['hrid']} keeps QA's repeat decision")


def stability_study(alice: requests.Session, project: dict[str, Any]) -> None:
    """Generate a study pull, draw its stored material and sign a trended measurement."""
    prefix = "iki/stability."
    give_lab_roles(alice, project, prefix + "coordinator", prefix + "reviewer", "iki/lots.qa")
    token = secrets.token_hex(4)
    # PubChem CID 2244 identifies aspirin. Its lot, report and test results here are synthetic.
    material = created(
        alice,
        project,
        "iki/small_molecule.Compound",
        {
            "name": "Aspirin",
            "smiles": "CC(=O)OC1=CC=CC=C1C(=O)O",
            "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
        },
    )
    lot = created(
        alice,
        project,
        "iki/lots.GenericLot",
        {"lot_number": f"Stability demo {token}", "of": entry_of(material)},
    )
    box = created(
        alice, project, INVENTORY + "Box", {"name": "Stability study", "rows": 1, "columns": 1}
    )
    tube = created(
        alice,
        project,
        INVENTORY + "Tube",
        {"barcode": f"DEMO-STABILITY-{token}", "contents": lot["id"]},
        amount={"value": "100", "unit": "uL"},
    )
    moved(alice, [(tube, box, "A1")], "Store study tube at its assigned position.")
    # ICH Q1A(R2) §2.1.7.1 general-case conditions:
    # https://database.ich.org/sites/default/files/Q1A%28R2%29%20Step4.pdf
    study = created(
        alice,
        project,
        prefix + "Study",
        {
            "name": "Aspirin stability demonstration",
            "material": entry_of(material),
            "lot": lot["id"],
            "protocol": "Synthetic practice study",
            "protocol_revision": "1",
            "owner": api_call(alice, "GET", "/me").json()["id"],
            "starts_on": datetime.now(UTC).date().isoformat(),
            "conditions": [
                {
                    "code": "long_term",
                    "label": "Long term",
                    "temperature": 25,
                    "temperature_tolerance": 2,
                    "humidity": 60,
                    "humidity_tolerance": 5,
                }
            ],
            "time_points": [{"month": 0, "days_before": 0, "days_after": 1}],
            "assignments": [
                {"condition": "long_term", "month": 0, "tube": tube["id"], "volume_ul": 12}
            ],
            "tests": [{"test": "Demo peak area", "method": "Synthetic method v1", "unit": "AU"}],
        },
    )

    def visible(component: str) -> list[dict[str, Any]]:
        answer = api_call(
            alice, "POST", f"/components/{prefix}{component}/entities", {"entity": study["id"]}
        )
        if answer.status_code != 200:
            fail(f"{component} answered HTTP {answer.status_code}: {answer.text}")
        found: list[dict[str, Any]] = answer.json()["items"]
        return found

    launch(alice, project, prefix + "schedule_pulls", {"study": study["id"]})
    [pull] = visible("pull_schedule")
    if (pull["values"]["status"], pull["values"]["tube"]) != ("scheduled", tube["id"]):
        fail(f"the study did not schedule its stored tube: {pull}")
    path = f"/entities/{tube['id']}/amount/draw"
    draw = {
        "amount": {"value": 12, "unit": "uL"},
        "purpose": "qa_sampling",
        "used_in": pull["id"],
        "confirm": tube["values"]["barcode"],
    }
    drawn = api_call(alice, "POST", path, draw)
    if drawn.status_code != 200:
        fail(f"drawing study material answered HTTP {drawn.status_code}: {drawn.text}")
    movement = drawn.json()["movement"]
    if (
        movement["source"] != lot["id"]
        or movement["used_in"] != pull["id"]
        or drawn.json()["amount"]["value"] != "88"
        or api_call(alice, "POST", path, draw).status_code != 422
    ):
        fail(f"the ledger did not record exactly one draw for the pull: {drawn.json()}")
    path = f"/entities/{pull['id']}"
    etag = api_call(alice, "GET", path).headers["ETag"]
    changed = api_call(
        alice,
        "PATCH",
        path,
        {"values": {"pulled_on": movement["created_at"][:10]}},
        {"If-Match": etag},
    )
    if changed.status_code != 200:
        fail(f"recording the pull date answered HTTP {changed.status_code}: {changed.text}")
    done = api_call(
        alice,
        "POST",
        path + "/states/status",
        {"state": "pulled"},
        {"If-Match": changed.headers["ETag"]},
    )
    if done.status_code != 200:
        fail(f"reconciling the pull answered HTTP {done.status_code}: {done.text}")
    report = upload(
        alice, project, "synthetic-stability-report.csv", b"test,peak area\ndemo,1.25\n", "text/csv"
    )
    launch(
        alice,
        project,
        prefix + "record_measurement",
        {"pull": pull["id"]},
        {"test": "Demo peak area", "value": 1.25, "report": report},
    )
    if visible("measurement_trend"):
        fail("the study trended a draft measurement")
    response = api_call(
        alice,
        "POST",
        "/entities/search",
        {
            "project": project["id"],
            "type": prefix + "Measurement",
            "where": {"field": "pull", "op": "eq", "value": pull["id"]},
        },
    )
    if response.status_code != 200:
        fail(f"looking up the measurement returned HTTP {response.status_code}: {response.text}")
    [measurement] = response.json()["items"]
    witness = signing(alice, measurement, "signed", "alice", field="review")
    [point] = visible("measurement_trend")
    if witness["status"] != "signed" or point["values"]["value"] != 1.25:
        fail(f"the signed stability measurement was not trended: {point}")
    ok(f"{study['hrid']} scheduled and drew its stored tube, then trended its signed result")


def register_puc19(
    alice: requests.Session, project: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Register the public reference used by the plasmid and sequencing scenarios."""
    run = launch(
        alice,
        project,
        "iki/plasmid.register_plasmid",
        {},
        {"bases": PUC19, "name": "pUC19", "accession": "L09137.2"},
    )
    plasmid = next(e for e in run["effects"] if e["type"] == "iki/plasmid.Plasmid")
    return run, record(alice, plasmid["entity"])


def plasmids(alice: requests.Session, project: dict[str, Any]) -> None:
    """The plasmid plugin, in its environment with Biopython: pUC19 registered from the
    2,686 bases of L09137.2 and annotated by its trigger from the library of common vector
    parts, at the coordinates pUC19's published map gives; the same molecule read from
    another origin on the other strand finding the entries it has already; a plasmid under
    a material transfer agreement having to name it (ADR 0127); and a cloning plan that
    puts pUC19's own polylinker back between its EcoRI and HindIII sites making pUC19
    again — the same molecule, so the registry gives back the entry it has (ADR 0138)."""
    annotating = "iki/plasmid.annotate_new_plasmid"
    on = api_call(alice, "POST", f"/projects/{project['id']}/triggers/{annotating}/enable", {})
    if on.status_code != 200 or not on.json().get("enabled"):
        fail(f"turning {annotating} on answered HTTP {on.status_code}: {on.text[:300]}")
    action = "iki/plasmid.register_plasmid"
    run, plasmid = register_puc19(alice, project)
    kinds = {e["type"]: e["kind"] for e in run["effects"]}
    if kinds.get("iki/plasmid.Plasmid") != "created":
        fail(f"{run['hrid']} ({action}) made {run['effects']}, not a plasmid")
    # Its trigger annotates it: the run is launched by the server, so wait for its values.
    deadline = time.monotonic() + 180
    values: dict[str, Any] = {}
    while time.monotonic() < deadline:
        values = record(alice, plasmid["id"])["values"]
        if values.get("annotated_with"):
            break
        time.sleep(2)
    else:
        fail(f"{plasmid['hrid']} wasn't annotated by its trigger within 180 seconds")
    found = {f["name"]: (f["start"], f["end"], f["strand"]) for f in values["annotated_features"]}
    expected = {
        "ori": (867, 1455, "reverse"),
        "AmpR": (1626, 2486, "reverse"),
        "lac promoter": (508, 591, "reverse"),
    }
    if found != expected or values.get("length") != 2686:
        fail(f"{plasmid['hrid']} was annotated {found}, {values.get('length')} bp, not {expected}")
    if (values.get("bacterial_resistance"), values.get("replication_origins")) != (
        ["ampicillin"],
        ["pMB1/pUC"],
    ):
        fail(f"{plasmid['hrid']}'s parts say {values}, not ampicillin from a pMB1/pUC origin")
    # The trigger did it, not something else that writes those fields.
    listed = api_call(alice, "GET", f"/projects/{project['id']}/triggers").json()
    fired = next(t for t in listed if t["name"] == annotating)
    if not fired["last_fired_at"]:
        fail(f"{plasmid['hrid']} was annotated, but {annotating} says it never fired")
    ok(
        f"{action} registered pUC19 as {plasmid['hrid']} and the trigger alice turned on "
        f"annotated it ({values['annotated_with']}): AmpR 1626..2486, ori 867..1455, "
        f"lac promoter 508..591"
    )
    # A rule holds at every save, however it's written: an MTA must be named (ADR 0127).
    etag = api_call(alice, "GET", f"/entities/{plasmid['id']}").headers["ETag"]
    unnamed = api_call(
        alice,
        "PATCH",
        f"/entities/{plasmid['id']}",
        {"values": {"mta_required": True}},
        {"If-Match": etag},
    )
    said = (
        [(e["pointer"], e["detail"]) for e in unnamed.json().get("errors", [])]
        if unnamed.status_code == 422
        else []
    )
    if not any(at.endswith("/mta_reference") and "name the agreement" in why for at, why in said):
        fail(f"an unnamed MTA answered HTTP {unnamed.status_code}: {unnamed.text[:300]}")
    named = api_call(
        alice,
        "PATCH",
        f"/entities/{plasmid['id']}",
        {
            "values": {
                "mta_required": True,
                "mta_reference": "DEV-MTA-1",
                "mta_provider": "Example University",
            }
        },
        {"If-Match": etag},
    )
    if named.status_code != 200:
        fail(f"naming the MTA answered HTTP {named.status_code}: {named.text[:300]}")
    ok(f"{plasmid['hrid']}: an MTA has to be named, and DEV-MTA-1 saves ({unnamed.status_code})")
    # The same molecule from another origin, on the other strand: the entries it has.
    rotated = PUC19[1343:] + PUC19[:1343]
    other = rotated.translate(str.maketrans("ACGT", "TGCA"))[::-1]
    run = launch(alice, project, action, {}, {"bases": other})
    again = {e["type"]: (e["kind"], e["entity"]) for e in run["effects"]}
    if again.get("iki/plasmid.Plasmid") != ("reused", plasmid["id"]):
        fail(f"{run['hrid']} made {run['effects']}, not {plasmid['hrid']} reused")
    ok(
        f"pUC19 read from base 1344 on the other strand is {plasmid['hrid']}, "
        f"reused ({run['hrid']})"
    )
    read_bases(alice, project, plasmid)
    assembled(alice, project, plasmid)
    strain_stocks(alice, project, plasmid)


#: NEB's M13/pUC sequencing primers (#S1201S and #S1233S), the pair pUC19's polylinker is
#: read and screened with.
M13_FORWARD = "GTAAAACGACGGCCAGT"
M13_REVERSE = "CAGGAAACAGCTATGAC"


def read_bases(alice: requests.Session, project: dict[str, Any], plasmid: dict[str, Any]) -> None:
    """What pUC19's own bases say, and what its primers do (ADR 0172), in the plugin's
    environment on the cluster: its beta-lactamase gene as an open reading frame of 286
    codons at 1626..2486 on the reverse strand, read by translation table 11; the M13/pUC
    sequencing primers binding it once each, where the record's bases put them; and the two
    of them amplifying 103 bp of its polylinker, registered as the linear DNA sequence a
    cloning plan can use as a fragment."""
    run = launch(
        alice,
        project,
        "iki/plasmid.find_orfs",
        {"plasmid": plasmid["id"]},
        {"least_codons": 75, "genetic_code": "bacterial_archaeal_plant_plastid"},
    )
    values = record(alice, plasmid["id"])["values"]
    bla = next(
        (orf for orf in values.get("orfs") or [] if (orf["start"], orf["end"]) == (1626, 2486)),
        None,
    )
    if bla is None or (bla["strand"], bla["amino_acids"], bla["frame"]) != ("reverse", 286, -3):
        fail(f"{run['hrid']} wrote ORFs {values.get('orfs')}, not pUC19's bla at 1626..2486")
    if not bla["translation"].startswith("MSIQHFRVALIP"):
        fail(f"{run['hrid']} translated bla as {bla['translation'][:30]}, not TEM-1's start")
    if "translation table 11" not in (values.get("orfs_found_with") or ""):
        fail(f"{run['hrid']} wrote orfs_found_with {values.get('orfs_found_with')!r}")
    ok(
        f"iki/plasmid.find_orfs read {plasmid['hrid']}'s bases ({run['hrid']}): "
        f"{len(values['orfs'])} ORFs of at least 75 codons, bla 1626..2486 on the reverse "
        "strand, 286 codons from ATG"
    )
    primers = []
    for bases, name in ((M13_FORWARD, "M13/pUC forward (-20)"), (M13_REVERSE, "M13/pUC reverse")):
        made = api_call(
            alice,
            "POST",
            "/entities",
            {
                "type": "iki/dna.Primer",
                "project": project["id"],
                "values": {"bases": bases, "name": name},
            },
        )
        if made.status_code not in (200, 201):
            fail(f"registering {name} answered HTTP {made.status_code}: {made.text[:300]}")
        primers.append(made.json())
    run = launch(
        alice,
        project,
        "iki/plasmid.find_primer_binding_sites",
        {"plasmid": plasmid["id"], "primers": [p["id"] for p in primers]},
        {"least_three_prime_match": 15, "most_mismatch_weight": 2},
    )
    values = record(alice, plasmid["id"])["values"]
    sites = {
        (s["strand"], s["start"], s["end"]): s for s in values.get("primer_binding_sites") or []
    }
    if set(sites) != {("forward", 379, 395), ("reverse", 465, 481)}:
        fail(f"{run['hrid']} found sites {list(sites)}, not the M13 pair's on pUC19")
    if sites[("forward", 379, 395)]["melting_temperature"] != 49.2:
        fail(f"{run['hrid']} wrote the forward site's Tm as {sites[('forward', 379, 395)]}")
    ok(
        f"iki/plasmid.find_primer_binding_sites placed both M13 primers on "
        f"{plasmid['hrid']} ({run['hrid']}): 379..395 forward at 49.2 C, 465..481 reverse"
    )
    reaction = api_call(
        alice,
        "POST",
        "/entities",
        {
            "type": "iki/plasmid.Pcr",
            "project": project["id"],
            "values": {
                "name": "pUC19 polylinker",
                "template": plasmid["values"]["sequence"],
                "forward_primer": entry_of(primers[0]),
                "reverse_primer": entry_of(primers[1]),
            },
        },
    )
    if reaction.status_code != 201:
        fail(f"recording a PCR answered HTTP {reaction.status_code}: {reaction.text[:300]}")
    pcr: dict[str, Any] = reaction.json()
    run = launch(alice, project, "iki/plasmid.simulate_pcr", {"pcr": pcr["id"]})
    amplified = next((e for e in run["effects"] if e["type"] == "iki/dna.DNASequence"), None)
    if amplified is None:
        fail(f"{run['hrid']} made {run['effects']}, not a DNA sequence for the product")
    product = record(alice, amplified["entity"])["values"]
    got = record(alice, pcr["id"])["values"]
    # Its length is computed by a run of its own (compute_composition): count its bases.
    made_bp = len(product.get("bases") or "")
    if product.get("topology") != "linear" or made_bp != 103:
        fail(f"{run['hrid']} registered {made_bp} bp {product.get('topology')}, not 103 linear")
    if not product["bases"].startswith(M13_FORWARD):
        begins = product["bases"][:30]
        fail(f"{run['hrid']}'s product begins {begins}, not with the forward primer")
    if (got.get("product_length"), got.get("products_found")) != (103, 1):
        fail(f"{run['hrid']} wrote {got}, not one 103 bp product")
    if (got.get("forward_melting_temperature"), got.get("reverse_melting_temperature")) != (
        49.2,
        43.6,
    ):
        fail(f"{run['hrid']} wrote the primers' Tms as {got}")
    # The product is the reaction's in the graph: derived from it, as an assembly's is.
    walked = api_call(alice, "GET", f"/entities/{pcr['id']}/downstream")
    below = {i["entity"]["id"] for i in walked.json().get("items", [])} if walked.ok else set()
    if amplified["entity"] not in below:
        fail(f"{pcr['hrid']}'s downstream ({walked.status_code}) doesn't hold its product")
    ok(
        f"iki/plasmid.simulate_pcr amplified {pcr['hrid']} ({run['hrid']}): 103 bp of "
        f"pUC19's polylinker, registered as the linear {amplified['entity'][:8]}…, Tm 49.2 "
        "and 43.6 C by SantaLucia 1998"
    )


def assembled(alice: requests.Session, project: dict[str, Any], plasmid: dict[str, Any]) -> None:
    """A cloning plan, worked out: pUC19 cut with EcoRI and HindIII keeps its 2,635 bp
    fragment, and its polylinker goes back in as the 51 bp piece between the two sites, so
    the plan makes 2,686 bp — pUC19 again, which the registry recognises. Then the clone is
    sequenced: stating its product fires the plan's other trigger, which finds that the
    plan makes what the clone has."""
    made = api_call(
        alice,
        "POST",
        "/entities",
        {
            "type": "iki/dna.DNASequence",
            "project": project["id"],
            "values": {"bases": POLYLINKER, "topology": "linear", "name": "pUC19 polylinker"},
        },
    )
    if made.status_code not in (200, 201):
        fail(f"registering the polylinker answered HTTP {made.status_code}: {made.text[:300]}")
    insert = made.json()
    plan = api_call(
        alice,
        "POST",
        "/entities",
        {
            "type": "iki/plasmid.Assembly",
            "project": project["id"],
            "values": {
                "name": "pUC19 polylinker back in",
                "method": "restriction_ligation",
                "enzymes": ["EcoRI", "HindIII"],
                "backbone": plasmid["values"]["sequence"],
                "inserts": [entry_of(insert)],
            },
        },
    )
    if plan.status_code != 201:
        fail(f"recording a cloning plan answered HTTP {plan.status_code}: {plan.text[:300]}")
    assembly: dict[str, Any] = plan.json()
    run = launch(alice, project, "iki/plasmid.assemble", {"assembly": assembly["id"]})
    effects = {e["type"]: e for e in run["effects"]}
    # What the plan makes is pUC19, so both of its entries are the ones Iki has already. An
    # effect names the project's record; its entry is what the plasmid's `sequence` holds.
    sequence = effects.get("iki/dna.DNASequence") or {}
    if sequence.get("kind") != "reused" or (
        entry_of(record(alice, sequence["entity"])) != plasmid["values"]["sequence"]
    ):
        fail(f"{run['hrid']} made {run['effects']}, not pUC19's own sequence reused")
    clone = effects.get("iki/plasmid.Plasmid") or {}
    if (clone.get("kind"), clone.get("entity")) != ("reused", plasmid["id"]):
        fail(f"{run['hrid']} made {run['effects']}, not {plasmid['hrid']} reused")
    got = record(alice, assembly["id"])["values"]
    if got.get("simulated_length") != 2686 or got.get("matches_product") is not None:
        fail(f"{run['hrid']} wrote {got}, not a 2686 bp circle with nothing to match yet")
    overhangs = sorted(j["overhang"] for j in got.get("junctions") or [])
    if overhangs != ["5' AATT", "5' AGCT"]:
        fail(f"{run['hrid']} wrote junctions {got.get('junctions')}, not the two sites'")
    ok(
        f"iki/plasmid.assemble worked out {assembly['hrid']} ({run['hrid']}): 2635 bp of "
        f"pUC19 and its 51 bp polylinker make 2686 bp, pUC19's own entries reused"
    )
    # The clone is sequenced: stating what it turned out to have fires the plan's other
    # trigger, whose run compares the two molecules.
    etag = api_call(alice, "GET", f"/entities/{assembly['id']}").headers["ETag"]
    stated = api_call(
        alice,
        "PATCH",
        f"/entities/{assembly['id']}",
        {"values": {"product": plasmid["values"]["sequence"]}},
        {"If-Match": etag},
    )
    if stated.status_code != 200:
        fail(f"stating the product answered HTTP {stated.status_code}: {stated.text[:300]}")
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        got = record(alice, assembly["id"])["values"]
        if got.get("matches_product") is not None:
            break
        time.sleep(2)
    else:
        fail(f"{assembly['hrid']}'s product wasn't checked by its trigger within 180 seconds")
    if got.get("matches_product") is not True:
        fail(f"{assembly['hrid']} says its plan makes {got}, not the sequence the clone has")
    ok(
        f"{assembly['hrid']}: the clone's sequence stated, and the trigger's check found "
        f"the plan makes it (2686 bp, the same molecule from any origin and either strand)"
    )


#: Inotersen (Tegsedi): a 5-10-5 2′-MOE gapmer, phosphorothioate throughout, every C a
#: 5-methylcytosine.
INOTERSEN = (
    "RNA1{"
    + "[sP].".join(
        f"[{'MOE' if i < 5 or i >= 15 else 'dR'}]({'[5meC]' if b == 'C' else b})"
        for i, b in enumerate("TCTTGGTTACATGAAATCCC")
    )
    + "}$$$$V2.0"
)
#: Hen egg-white lysozyme's 129 mature residues (UniProt P00698, 19-147).
LYSOZYME = (
    "KVFGRCELAAAMKRHGLDNYRGYSLGNWVCAAKFESNFNTQATNRNTDGSTDYGILQINSRWWCNDGRTPGSRNLCNIPCSA"
    "LLSSDITASVNCAKKIVSDGNGMNAWVAWRNRCKGTDVQAWIRGCRL"
)


def mouse_stocks(alice: requests.Session, project: dict[str, Any]) -> None:
    """Import MGI nomenclature and trace genotyping data to a specific cryopreserved carrier."""
    plugin = "iki/mouse_strains."
    catalogue = (
        Path(__file__).resolve().parents[2] / "plugins/mouse_strains/tests/data/MGI_Strain.rpt"
    )
    report = upload(alice, project, "MGI_Strain.rpt", catalogue.read_bytes(), "text/plain")
    imported = launch(
        alice,
        project,
        plugin + "register_mgi_strain",
        {},
        {"file": report, "mgi_id": "MGI:3028467"},
    )
    [effect] = imported["effects"]
    reference = record(alice, effect["entity"])["values"]
    # The MGI report has no repository stocks: a JAX stock comes from the JAX catalogue.
    if (reference["catalogue"], reference["nomenclature"], reference.get("jax_stock")) != (
        "mgi",
        "C57BL/6J",
        None,
    ):
        fail(f"{imported['hrid']} registered an unexpected catalogue strain {reference}")
    # https://www.jax.org/strain/000058 and https://www.informatics.jax.org/allele/MGI:1855985
    allele = created(
        alice, project, plugin + "MouseAllele", {"mgi_id": "MGI:1855985", "symbol": "Tyr<c-2J>"}
    )
    strain = created(
        alice,
        project,
        plugin + "MouseStrain",
        {
            "mgi_id": "MGI:2163869",
            "nomenclature": "B6(Cg)-Tyr<c-2J>/J",
            "jax_stock": "000058",
            "alleles": [entry_of(allele)],
        },
    )
    # All local specimens, dates, amounts and test results are synthetic.
    tag = secrets.token_hex(4)
    genotype = {
        "strain": entry_of(strain),
        "alleles": [entry_of(allele)],
        "notation": "Tyr<c-2J>/Tyr<c-2J>",
    }
    donor = created(
        alice, project, plugin + "MouseGenotype", {**genotype, "subject": f"Synthetic donor {tag}"}
    )
    banked = {"strain": entry_of(strain), "banked_on": "2026-01-08"}
    sperm = created(
        alice,
        project,
        plugin + "MouseCryostock",
        {
            **banked,
            "kind": "sperm",
            "carrier": "straw",
            "genotype": donor["id"],
            "barcode": f"SYNTHETIC-S-{tag}",
        },
    )
    embryo_code = f"SYNTHETIC-E-{tag}"
    embryos = created(
        alice,
        project,
        plugin + "MouseCryostock",
        {
            **banked,
            "kind": "embryos",
            "carrier": "cryovial",
            "embryos_at_banking": 4,
            "barcode": embryo_code,
        },
        {"value": 100, "unit": "uL"},
    )
    box = created(alice, project, plugin + "MouseCryobox", {"name": "Synthetic mouse cryobox"})
    moved(alice, [(sperm, box, "A1"), (embryos, box, "A2")], "Synthetic bank deposit")
    for stock, position in ((sperm, "A1"), (embryos, "A2")):
        location = api_call(alice, "GET", f"/entities/{stock['id']}/location").json()
        if [(step["id"], step["position"]) for step in location["path"]] != [(box["id"], position)]:
            fail(f"{stock['hrid']}'s cryobank location is {location}")
    assay_file = upload(
        alice,
        project,
        "synthetic-genotype.txt",
        b"Synthetic embryo genotype report\n",
        "text/plain",
    )
    tested = created(
        alice,
        project,
        plugin + "MouseGenotype",
        {**genotype, "subject": f"Synthetic embryo sample {tag}", "report": assay_file},
    )
    draw = api_call(
        alice,
        "POST",
        f"/entities/{embryos['id']}/amount/draw",
        {
            "amount": {"value": 10, "unit": "uL"},
            "confirm": embryo_code,
            "reason": "Synthetic genotyping",
            "used_in": tested["id"],
        },
    )
    if draw.status_code != 200 or Decimal(draw.json()["amount"]["value"]) != 90:
        fail(f"Draw for {tested['hrid']} answered HTTP {draw.status_code}: {draw.text[:300]}")
    ancestry = api_call(alice, "GET", f"/entities/{tested['id']}/graph?up=3&down=0")
    if ancestry.status_code != 200:
        fail(f"Graph for {tested['hrid']} gave HTTP {ancestry.status_code}: {ancestry.text[:500]}")
    graph = ancestry.json()
    if not {embryos["id"], strain["id"], allele["id"]} <= {e["id"] for e in graph["entities"]}:
        fail(f"{tested['hrid']}'s report lost the banked carrier, strain or MGI allele")
    ok(f"{tested['hrid']}'s genotyping report traces to {embryos['hrid']} from JAX stock 000058")


def lots(alice: requests.Session, project: dict[str, Any]) -> None:
    """The lots plugin, in its environment: an inotersen lot's mass by ESI-MS checked against
    the one its HELM gives, and its nmol drawn by volume at its concentration; and a
    lysozyme lot's concentration from its A280 and its sequence's ε280, with its cystines."""
    run = launch(alice, project, "iki/oligo.register_oligonucleotide", {}, {"helm": INOTERSEN})
    (oligo,) = [e for e in run["effects"] if e["type"] == "iki/oligo.Oligonucleotide"]
    weight = record(alice, oligo["entity"])["values"].get("molecular_weight")
    if not (isinstance(weight, (int, float)) and abs(weight - 7183.08) < 0.01):
        fail(f"{run['hrid']} registered inotersen at {weight} g/mol, not 7183.08")
    # Prakash et al. (2014), Table 1: 7182.2 found by ESI-MS (average mass).
    lot = created(
        alice,
        project,
        "iki/lots.OligoLot",
        {
            "oligonucleotide": oligo["entity"],
            "lot_number": f"TEG-{secrets.token_hex(3)}",
            "synthesis_scale": 1,
            "purification": "ie_hplc",
            "concentration": 100,
            "measured_mass": 7182.2,
            "mass_basis": "average",
            "mass_tolerance": 2,
        },
        {"value": 500, "unit": "nmol"},
    )
    run = launch(alice, project, "iki/lots.compute_oligo_lot", {"lot": lot["id"]})
    got = record(alice, lot["id"])["values"]
    if (got.get("expected_mass"), got.get("mass_error")) != (7183.08, -0.88):
        fail(f"{run['hrid']} checked {lot['hrid']}'s mass as {got}, not -0.88 Da from 7183.08")
    # The lot is in quarantine. A 50 uL sample for testing at 100 uM is 5 nmol.
    give_lab_roles(alice, project, "iki/lots.qa")
    drawn = api_call(
        alice,
        "POST",
        f"/entities/{lot['id']}/amount/draw",
        {"amount": {"value": 50, "unit": "uL"}, "purpose": "qa_sampling", "reason": "QC assay"},
    )
    if drawn.status_code != 200:
        fail(f"drawing from {lot['hrid']} answered HTTP {drawn.status_code}: {drawn.text[:300]}")
    movement = drawn.json()["movement"]
    if movement["purpose"] != "qa_sampling" or movement["user"]["email"] != "alice@iki.test":
        fail(f"{lot['hrid']}'s ledger did not record who took the QA sample and why")
    left = api_call(alice, "GET", f"/entities/{lot['id']}/amount").json()
    if Decimal(left["value"]) != 495:
        fail(f"{lot['hrid']} has {left} left, not 495 nmol")
    ok(
        f"iki/lots.compute_oligo_lot found {lot['hrid']}'s ESI-MS mass 0.88 Da from "
        f"inotersen's 7183.08, and 50 uL at 100 uM drew 5 nmol ({run['hrid']})"
    )
    made = api_call(
        alice,
        "POST",
        "/entities",
        {
            "type": "iki/protein.Protein",
            "project": project["id"],
            "values": {"name": "Lysozyme C (hen)", "sequence": LYSOZYME},
        },
    )
    if made.status_code not in (200, 201):
        fail(f"registering lysozyme answered HTTP {made.status_code}: {made.text[:300]}")
    protein = made.json()
    launch(alice, project, "iki/protein.compute_properties", {"protein": protein["id"]})
    lot = created(
        alice,
        project,
        "iki/lots.ProteinLot",
        {"protein": protein["id"], "a280": 0.53, "dilution_factor": 10, "path_length": 1},
        {"value": 20, "unit": "mg"},
    )
    run = launch(alice, project, "iki/lots.compute_protein_lot", {"lot": lot["id"]})
    got = record(alice, lot["id"])["values"]
    # Its 4 cystines take 8 H off its sequence's 14,313.0 g/mol: 37,970 / 14,304.94.
    if (got.get("a280_extinction"), got.get("concentration")) != (2.6543, 1.9967):
        fail(f"{run['hrid']} worked out {lot['hrid']} as {got}, not 1.9967 mg/mL at 2.6543")
    ok(
        f"iki/lots.compute_protein_lot: {lot['hrid']}'s A280 of 0.53, diluted 1 in 10, is "
        f"1.9967 mg/mL of lysozyme at its cystines' 2.6543 mL/(mg·cm) ({run['hrid']})"
    )


def created(
    alice: requests.Session,
    project: dict[str, Any],
    kind: str,
    values: dict[str, Any],
    amount: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A new record of ``kind``, with ``amount`` recorded."""
    body: dict[str, Any] = {"type": kind, "project": project["id"], "values": values}
    if amount is not None:
        body["amount"] = amount
    made = api_call(alice, "POST", "/entities", body)
    if made.status_code not in (200, 201):
        fail(f"recording a {kind} answered HTTP {made.status_code}: {made.text[:300]}")
    entity: dict[str, Any] = made.json()
    return entity


def pubchem(alice: requests.Session, project: dict[str, Any]) -> dict[str, Any]:
    """Caffeine (PubChem CID 2519), fetched from PubChem by name through the egress proxy,
    and registered by its InChIKey with its CID, name and CAS number, its origin saying
    which record it came from."""
    action = "iki/small_molecule.fetch_from_pubchem"
    run = launch(alice, project, action, {}, {"identifiers": "caffeine"}, within=300)
    if run["network"] != ["pubchem.ncbi.nlm.nih.gov"]:
        fail(f"{run['hrid']} may reach {run['network']}, not PubChem alone")
    attempt = api_call(alice, "GET", f"/runs/{run['id']}/attempts").json()[-1]
    reached = {(e["host"], e["port"], e["refused"]) for e in attempt["network"]}
    if reached != {("pubchem.ncbi.nlm.nih.gov", 443, None)}:
        fail(f"{run['hrid']} reached {reached}, not PubChem alone")
    made = [e for e in run["effects"] if e["type"] == "iki/small_molecule.Compound"]
    if len(made) != 1:
        fail(f"{run['hrid']} ({action}) made {run['effects']}, not caffeine")
    compound = record(alice, made[0]["entity"])
    values = compound["values"]
    expected = {
        "inchikey": CAFFEINE_KEY,
        "pubchem_cid": "2519",
        "name": "Caffeine",
        "cas_number": "58-08-2",
    }
    if {k: values.get(k) for k in expected} != expected:
        fail(f"{run['hrid']} registered caffeine as {values}")
    origins = api_call(alice, "GET", f"/entities/{compound['id']}/origins").json()
    if not any(
        o["source"] == "PubChem"
        and o["id"] == "2519"
        and o["url"] == "https://pubchem.ncbi.nlm.nih.gov/compound/2519"
        for o in origins
    ):
        fail(f"{compound['hrid']}'s origins are {origins}, not PubChem's CID 2519")
    ok(
        f"{action} fetched caffeine through the egress proxy as {compound['hrid']} "
        f"({CAFFEINE_KEY}, CID 2519, CAS 58-08-2) ({run['hrid']})"
    )
    return compound


def substructure(
    alice: requests.Session,
    project: dict[str, Any],
    aspirin: dict[str, Any],
    caffeine: dict[str, Any],
) -> None:
    """iki/small_molecule.substructure_search over aspirin and caffeine: a carboxylic acid,
    which only aspirin has, saved as a collection of it."""
    action = "iki/small_molecule.substructure_search"
    smarts = "[CX3](=O)[OX2H1]"
    run = launch(
        alice, project, action, {"compounds": [aspirin["id"], caffeine["id"]]}, {"smarts": smarts}
    )
    made = [e for e in run["effects"] if e["type"] == "iki/core.Collection"]
    if len(made) != 1 or run["metrics"] != {"hits": 1, "searched": 2}:
        fail(f"{run['hrid']} ({action}) made {run['effects']} with {run['metrics']}")
    collection = record(alice, made[0]["entity"])
    if collection["values"].get("members") != [entry_of(aspirin)]:
        fail(f"{run['hrid']} made {collection['hrid']} of {collection['values']}, not aspirin")
    ok(
        f"{action} found the carboxylic acid ({smarts}) in aspirin, not caffeine, and saved "
        f"it as {collection['hrid']} ({run['hrid']})"
    )


#: Human haemoglobin's alpha and beta chains (UniProt P69905, P68871).
HBA = (
    "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHFDLSHGSAQVKGHGKKVADALTNAVAHVDDMPNALSALSDLHAH"
    "KLRVDPVNFKLLSHCLLVTLAAHLPAEFTPAVHASLDKFLASVSTVLTSKYR"
)
HBB = (
    "MVHLTPEEKSAVTALWGKVNVDEVGGEALGRLLVVYPWTQRFFESFGDLSTPDAVMGNPKVKAHGKKVLGAFSDGLAHLDNLKGTF"
    "ATLSELHCDKLHVDPENFRLLGNVLVCVLAHHFGKEFTPPVQAAYQKVVAGVANALAHKYH"
)


def aligned(alice: requests.Session, project: dict[str, Any]) -> list[dict[str, Any]]:
    """iki/protein.align_pair on haemoglobin's chains: its metrics on the run, and its alignment
    kept as artifacts (ADR 0068), downloaded from where Iki's URL sends a browser. The
    chains, for aligned_many."""
    chains = []
    human = organism(alice, project, 9606, "Homo sapiens")
    for name, sequence in (("HBA_HUMAN", HBA), ("HBB_HUMAN", HBB)):
        made = api_call(
            alice,
            "POST",
            "/entities",
            {
                "type": "iki/protein.Protein",
                "project": project["id"],
                "values": {"name": name, "sequence": sequence, "organism": human},
            },
        )
        if made.status_code not in (200, 201):
            fail(f"registering {name} answered HTTP {made.status_code}: {made.text[:300]}")
        chains.append(made.json())
    # Not reused: this check is of a run that keeps its artifacts.
    body = {
        "project": project["id"],
        "inputs": {"query": chains[0]["id"], "target": chains[1]["id"]},
        "reuse": False,
    }
    launched = api_call(alice, "POST", "/actions/iki/protein.align_pair/runs", body)
    if launched.status_code != 201:
        fail(f"launching iki/protein.align_pair answered HTTP {launched.status_code}")
    run = followed(alice, launched.json(), within=120)
    if run["status"] != "succeeded":
        fail(f"{run['hrid']} (iki/protein.align_pair) {run['status']}: {run['error']}")
    metrics = run.get("metrics") or {}
    # The run keeps percentages unrounded: 65 identical of 149 columns.
    identity = metrics.get("identity_percent")
    if (
        not isinstance(identity, (int, float))
        or abs(identity - 100 * 65 / 149) > 1e-9
        or metrics.get("length") != 149
    ):
        fail(f"{run['hrid']} measured {metrics}, not 65/149 identical columns (43.6%)")
    kept = api_call(alice, "GET", f"/runs/{run['hrid']}/artifacts").json()
    if sorted((k["path"], k["state"]) for k in kept) != [
        ("alignment.fasta", "kept"),
        ("alignment.txt", "kept"),
    ]:
        fail(f"{run['hrid']} kept {kept}, not its alignment")
    # The FASTA is also an alignment of the two in Iki (ADR 0088), the target its reference.
    created = [e for e in run.get("effects") or [] if e["kind"] == "created"]
    if [e["type"] for e in created] != ["iki/core.Alignment"]:
        fail(f"{run['hrid']} created {created}, not one iki/core.Alignment")
    alignment = record(alice, created[0]["entity"])
    values = alignment["values"]
    if values["members"] != [entry_of(chains[0]), entry_of(chains[1])] or values[
        "reference"
    ] != entry_of(chains[1]):
        fail(f"{created[0]['hrid']} aligns {values['members']} to {values['reference']}")
    fasta = next(k for k in kept if k["path"] == "alignment.fasta")
    stored = api_call(alice, "GET", f"/files/{values['aligned']}/content-url").json()
    if stored["sha256"] != fasta["sha256"]:
        fail(f"{created[0]['hrid']}'s file isn't the alignment.fasta {run['hrid']} kept")
    text = next(k for k in kept if k["path"] == "alignment.txt")
    got = api_call(alice, "GET", f"/runs/{run['hrid']}/artifacts/{text['id']}/content")
    if got.status_code != 200 or hashlib.sha256(got.content).hexdigest() != text["sha256"]:
        fail(f"downloading {run['hrid']}'s alignment.txt answered HTTP {got.status_code}")
    if b"# Identity:   65/149 (43.6%)" not in got.content:
        fail(f"{run['hrid']}'s alignment.txt says {got.content[:400]!r}")
    ok(
        f"iki/protein.align_pair aligned haemoglobin's chains ({run['hrid']}): "
        f"{identity:.1f}% identical, kept {len(kept)} artifacts "
        f"until {text['expires_at'][:10]}, and made {created[0]['hrid']} of the two"
    )
    return chains


#: A codon for each residue, to write the bases that code for a protein.
CODONS = {
    "A": "GCC",
    "C": "TGC",
    "D": "GAC",
    "E": "GAG",
    "F": "TTC",
    "G": "GGC",
    "H": "CAC",
    "I": "ATC",
    "K": "AAG",
    "L": "CTG",
    "M": "ATG",
    "N": "AAC",
    "P": "CCC",
    "Q": "CAG",
    "R": "CGC",
    "S": "AGC",
    "T": "ACC",
    "V": "GTG",
    "W": "TGG",
    "Y": "TAC",
}


def reads_on_protein(alice: requests.Session, project: dict[str, Any], hbb: dict[str, Any]) -> None:
    """iki/protein.align_reads: a read of the start of haemoglobin beta's code, through the
    vector before it, on the other strand, with the sickle-cell change (E7V), found in its
    frame and named in the protein's numbering."""
    coded = "".join(CODONS[r] for r in "MVHLTPV" + HBB[7:100])
    vector = "".join(secrets.choice("ACGT") for _ in range(80))
    read = (vector + coded).translate(str.maketrans("ACGT", "TGCA"))[::-1]
    fastq = f"@hbb_R1\n{read}\n+\n{'I' * len(read)}\n".encode()
    files = [upload(alice, project, "hbb_R1.fastq", fastq, "text/x-fastq")]
    action = "iki/protein.align_reads"
    run = launch(alice, project, action, {"reference": hbb["id"]}, {"reads": files})
    metrics = run.get("metrics") or {}
    if metrics.get("aligned") != 1 or metrics.get("differences") != 1:
        fail(f"{run['hrid']} ({action}) measured {metrics}, not 1 read and 1 difference")
    said = [m["text"] for m in run.get("messages") or []]
    expected = f"Differences from {hbb['hrid']}: p.(E7V) (hbb_R1 Q40)."
    if expected not in said:
        fail(f"{run['hrid']} said {said}, not {expected!r}")
    created = [e for e in run.get("effects") or [] if e["kind"] == "created"]
    if [e["type"] for e in created] != ["iki/core.Alignment"]:
        fail(f"{run['hrid']} created {created}, not one iki/core.Alignment")
    values = record(alice, created[0]["entity"])["values"]
    if values["reference"] != entry_of(hbb) or values["members"] != [entry_of(hbb)]:
        fail(f"{created[0]['hrid']} is {values}")
    ok(
        f"{action} found haemoglobin beta's code in a read's frame on the other strand "
        f"({run['hrid']}): {metrics['coverage_percent']}% covered, E7V, making "
        f"{created[0]['hrid']}"
    )


#: Human myoglobin (UniProt P02144), a globin like haemoglobin's chains.
MB = (
    "MGLSDGEWQLVLNVWGKVEADIPGHGQEVLIRLFKGHPETLEKFDKFKHLKSEDEMKASEDLKKHGATVLTALGGILKKKGHHEAE"
    "IKPLAQSHATKHKIPVKYLEFISECIIQVLQSKHPGDFGADAQGAMNKALELFRKDMASNYKELGFQG"
)
#: The protein plugin's alignment-tools image (plugins/protein/environments/alignment-tools.yaml).
ALIGNMENT_TOOLS = (
    "ghcr.io/yogesh-dhande/iki-protein-alignment-tools"
    "@sha256:74cb2a5405950c42efa5a5edf049e2c571f8b7790338ab7eed3fcdf521e70dbd"
)
#: The dna plugin's alignment-tools image (plugins/dna/environments/alignment-tools.yaml).
DNA_ALIGNMENT_TOOLS = (
    "ghcr.io/yogesh-dhande/iki-dna-alignment-tools"
    "@sha256:a6792250dfaa87d5c2398b24c6d33d144a646053c63984481be17706ffdbda39"
)

#: Trastuzumab's variable domains (PDB 1N8Z), a humanized IgG1 against HER2.
TRASTUZUMAB_VH = (
    "EVQLVESGGGLVQPGGSLRLSCAASGFNIKDTYIHWVRQAPGKGLEWVARIYPTNGYTRYADSVKGRFTISADTSKNTAYLQMNSLR"
    "AEDTAVYYCSRWGGDGFYAMDYWGQGTLVTVSS"
)
TRASTUZUMAB_VL = (
    "DIQMTQSPSSLSASVGDRVTITCRASQDVNTAVAWYQQKPGKAPKLLIYSASFLYSGVPSRFSGSRSGTDFTLTISSLQPEDFATYY"
    "CQQHYTTPPTFGQGTKVEIK"
)


#: Trastuzumab's Fv as the complex plugin computes it from its variable domains (each
#: cysteine's thiol free): formula, average and monoisotopic masses, chain count, and
#: molar absorbance at 280 nm (Pace's, as Biopython's ProtParam gives its chains: 35,410
#: and 14,440).
FV_COMPOSITION = ("C1105H1687N297O344S7", 24860.46, 24845.1689, 2, 49850)
#: The antibody plugin's numbering-tools image (plugins/antibody/environments/numbering-tools.yaml).
NUMBERING_TOOLS = (
    "ghcr.io/yogesh-dhande/iki-antibody-numbering-tools"
    "@sha256:c314f625ee3c381add9a9cfcba3596cd7e0b61529764dc906c4fb09902007384"
)
#: The ab_predict plugin's ImmuneBuilder image (its environments/structure-tools.yaml).
IMMUNEBUILDER_TOOLS = (
    "ghcr.io/yogesh-dhande/iki-antibody-structure-tools"
    "@sha256:eb6f0596205fa9fac71d5909b25e992668854599fb8e53e60ab5e88604b81e85"
)


def save_app(
    alice: requests.Session, project: dict[str, Any], name: str, spec: dict[str, Any]
) -> requests.Response:
    """Saves the project's app of that name: a new revision of it if it's there, as
    someone editing it would, so running the seed again doesn't add another."""
    search = {
        "type": "iki/core.App",
        "exact_type": False,
        "project": project["id"],
        "limit": 1,
        "where": {"field": "name", "op": "eq", "value": name},
    }
    found = api_call(alice, "POST", "/entities/search", search).json().get("items", [])
    if not found:
        values = {"name": name, "spec": spec}
        body = {"type": "iki/core.App", "project": project["id"], "values": values}
        return api_call(alice, "POST", "/entities", body)
    hrid = found[0]["hrid"]
    etag = api_call(alice, "GET", f"/entities/{hrid}").headers.get("ETag", "")
    return api_call(
        alice, "PATCH", f"/entities/{hrid}", {"values": {"spec": spec}}, {"If-Match": etag}
    )


def organism(alice: requests.Session, project: dict[str, Any], taxon: int, name: str) -> str:
    """The organism with that NCBI Taxonomy ID (ADR 0140), registered in the project the first
    time, so a sequence's organism is a reference to it, and running the seed again reuses it."""
    search = {
        "type": "iki/core.Organism",
        "project": project["id"],
        "limit": 1,
        "where": {"field": "taxon_id", "op": "eq", "value": taxon},
    }
    found = api_call(alice, "POST", "/entities/search", search).json().get("items", [])
    if found:
        return str(found[0]["id"])
    values = {"taxon_id": taxon, "scientific_name": name}
    body = {"type": "iki/core.Organism", "project": project["id"], "values": values}
    made = api_call(alice, "POST", "/entities", body)
    if made.status_code not in (200, 201):
        fail(
            f"registering {name} (taxon {taxon}) answered HTTP {made.status_code}: "
            f"{made.text[:300]}"
        )
    return str(made.json()["id"])


def globins_app(alice: requests.Session, project: dict[str, Any]) -> None:
    """An app (ADR 0090): a grid of the globins, the one picked in the sequence viewer, and
    a line of text, bound by expressions the server checks with iki-wasm in its pod."""
    grid = {
        "id": "globins",
        "title": "Globins",
        "width": 6,
        "component": "iki.entities",
        "props": {
            "type": "iki/protein.Protein",
            "where": {
                "all": [
                    {"field": "name", "op": "in", "value": ["HBA_HUMAN", "HBB_HUMAN", "MB_HUMAN"]}
                ]
            },
            "sort": "name",
            "columns": [
                {"field": "name"},
                {"label": "Length", "value": "{{ size(row.values.sequence) }} aa"},
            ],
        },
    }
    viewer = {
        "id": "picked",
        "title": "Picked",
        "width": 6,
        "component": "iki.viewer",
        "props": {"entity": "{{ globins.selection }}", "viewer": "sequence"},
    }
    text = {
        "id": "summary",
        "component": "iki.text",
        "props": {
            "text": "{{ size(globins.rows) }} globins; {{ size(globins.selection) }} picked."
        },
    }

    def save(*panels: dict[str, Any]) -> requests.Response:
        return save_app(alice, project, "Globins", {"version": 1, "panels": list(panels)})

    wrong = save(text, grid)
    details = (
        [e["detail"] for e in wrong.json().get("errors", [])] if wrong.status_code == 422 else []
    )
    if not any("`globins` isn’t a panel before it" in d for d in details):
        fail(f"an app reading a later panel answered HTTP {wrong.status_code}: {wrong.text[:300]}")
    made = save(grid, viewer, text)
    if made.status_code not in (200, 201):
        fail(f"saving an app answered HTTP {made.status_code}: {made.text[:300]}")
    ok(f"{made.json()['hrid']}: an app of three panels, its expressions checked by iki-wasm")


def dashboard(alice: requests.Session, project: dict[str, Any], relatives: str) -> None:
    """The project's page opens with Globins (ADR 0090), which Alice, its Admin, chooses.
    An app about one protein can't be the dashboard: it's a tab on their pages."""
    path = f"/projects/{project['id']}/dashboard"
    choices = {a["name"]: a["hrid"] for a in api_call(alice, "GET", f"{path}/choices").json()}
    if "Globins" not in choices or "Relatives" in choices:
        fail(f"the dashboard's choices are {sorted(choices)}, not Globins and no Relatives")
    refused = api_call(alice, "PUT", path, {"app": relatives})
    if refused.status_code != 409:
        fail(f"an app about a protein as the dashboard answered HTTP {refused.status_code}")
    chosen = api_call(alice, "PUT", path, {"app": choices["Globins"]})
    if chosen.status_code != 200 or (chosen.json()["app"] or {}).get("name") != "Globins":
        fail(f"choosing the dashboard answered HTTP {chosen.status_code}: {chosen.text[:300]}")
    ok(f"{choices['Globins']}: {project['name']}’s page opens with it")


def app_draft(alice: requests.Session, project: dict[str, Any]) -> None:
    """A draft of an app (ADR 0090), checked as saving it would be, that only Alice, whom
    it's for, can open; Bob can't, though he signs in to the same Iki."""
    spec = {
        "version": 1,
        "panels": [{"id": "hi", "component": "iki.text", "props": {"text": "Hello, {{ 1 + 1 }}."}}],
    }
    body = {"project": project["id"], "name": "Draft", "spec": spec}
    made = api_call(alice, "POST", "/apps/drafts", body)
    if made.status_code != 201:
        fail(f"drafting an app answered HTTP {made.status_code}: {made.text[:300]}")
    draft = made.json()
    path = f"/apps/drafts/{draft['id']}"
    if api_call(alice, "GET", path).status_code != 200:
        fail("Alice can't open the draft she made")
    seen = api_call(sign_in_as("bob"), "GET", path).status_code
    if seen != 404:
        fail(f"Bob opening Alice's draft answered HTTP {seen}, not 404")
    ok(f"a draft app only Alice can open, at /projects/{project['slug']}/apps/drafts/{draft['id']}")


def relatives_app(alice: requests.Session, project: dict[str, Any]) -> str:
    """An app about a protein (ADR 0090): a tab on each protein's page in the project, its
    grid's conditions worked out from the protein it's on."""
    spec = {
        "version": 1,
        "entity": {"type": "iki/protein.Protein"},
        "panels": [
            {
                "id": "length",
                "component": "iki.text",
                "props": {
                    "text": "{{ entity.values.name }} has {{ size(entity.values.sequence) }} "
                    "residues. Others from the same organism:"
                },
            },
            {
                "id": "kin",
                "title": "From the same organism",
                "component": "iki.entities",
                "props": {
                    "type": "iki/protein.Protein",
                    "select": "none",
                    "where": {
                        "all": [
                            {
                                "field": "organism",
                                "op": "eq",
                                "value": "{{ entity.values.organism }}",
                            },
                            {"field": "name", "op": "ne", "value": "{{ entity.values.name }}"},
                        ]
                    },
                    "sort": "name",
                    "limit": 10,
                    "columns": [
                        {"field": "name"},
                        {"label": "Length", "value": "{{ size(row.values.sequence) }} aa"},
                    ],
                },
            },
        ],
    }
    made = save_app(alice, project, "Relatives", spec)
    if made.status_code not in (200, 201):
        fail(f"saving an app about a protein answered HTTP {made.status_code}: {made.text[:300]}")
    search = {
        "type": "iki/protein.Protein",
        "exact_type": False,
        "limit": 1,
        "where": {"field": "name", "op": "eq", "value": "HBB_HUMAN"},
    }
    found = api_call(alice, "POST", "/entities/search", search).json()["items"]
    if not found:
        fail("HBB_HUMAN wasn't found for its app")
    tabs = api_call(alice, "GET", f"/entities/{found[0]['id']}/apps").json()
    if made.json()["hrid"] not in [t["hrid"] for t in tabs]:
        fail(f"HBB_HUMAN's page doesn't list its app: {tabs}")
    ok(f"{made.json()['hrid']}: an app about a protein, a tab on {found[0]['hrid']}'s page")
    hrid: str = made.json()["hrid"]
    return hrid


def antibodies(alice: requests.Session, project: dict[str, Any]) -> None:
    """The antibody plugin: trastuzumab's chains numbered by ANARCI, in a pod of their own
    with the plugin's image; then its Fv assembled from them, on the backend."""
    chains = []
    for name, sequence in (("Trastuzumab VH", TRASTUZUMAB_VH), ("Trastuzumab VL", TRASTUZUMAB_VL)):
        made = api_call(
            alice,
            "POST",
            "/entities",
            {
                "type": "iki/protein.Protein",
                "project": project["id"],
                "values": {"name": name, "sequence": sequence},
            },
        )
        if made.status_code not in (200, 201):
            fail(f"registering {name} answered HTTP {made.status_code}: {made.text[:300]}")
        chains.append(made.json())
    heavy, light = chains
    expected = {
        heavy["id"]: ("heavy", "cdr_h3", "SRWGGDGFYAMDY", "heavy_v_gene", "IGHV3-66*01"),
        light["id"]: ("kappa", "cdr_l3", "QQHYTTPPT", "light_v_gene", "IGKV1-39*01"),
    }
    for chain in chains:
        # The first pulls the image into a pod of its own.
        run = launch(
            alice, project, "iki/antibody.number_chains", {"protein": chain["id"]}, within=900
        )
        (attempt,) = api_call(alice, "GET", f"/runs/{run['id']}/attempts").json()
        if attempt["environment_build"] != NUMBERING_TOOLS.rpartition("@")[2]:
            fail(f"{run['hrid']} didn't run in the numbering-tools image: {attempt}")
        if attempt["template"] != "container-numbering":
            fail(f"{run['hrid']} didn't run on the numbering template: {attempt}")
        got = api_call(alice, "GET", f"/entities/{chain['id']}").json()["values"]
        kind, cdr, cdr3, gene, v_gene = expected[chain["id"]]
        identity = got.get(gene.replace("v_gene", "germline_identity"))
        if (
            (got.get("chain_type"), got.get(cdr), got.get(gene), got.get("species"))
            != (kind, cdr3, v_gene, "human")
            or got.get("germline_set") != "ANARCI 2026.2.13.2, IMGT germlines"
            or got.get("numbering_scheme") != "imgt"
            or not isinstance(identity, (int, float))
            or not 80 < identity < 90
        ):
            fail(f"{run['hrid']} numbered {chain['hrid']} as {got}")
        ok(
            f"iki/antibody.number_chains ran ANARCI in a pod of its own ({run['hrid']}): "
            f"{chain['hrid']} is a {kind} chain, CDR3 {cdr3}, {v_gene} ({identity}%)"
        )
    # The complex plugin computes each new complex's composition once its Admin turns its
    # trigger on in the project (ADR 0080), as they would in the web app.
    enabled = api_call(
        alice,
        "POST",
        f"/projects/{project['id']}/triggers/iki/complex.compute_new_complex_composition/enable",
        {},
    )
    if enabled.status_code != 200:
        fail(
            f"turning on iki/complex.compute_new_complex_composition answered HTTP "
            f"{enabled.status_code}: {enabled.text[:300]}"
        )
    # Its variable domains alone, numbered first: an Fv, with no constant region, so no
    # isotype.
    params = {"name": "Trastuzumab", "format": "fv", "target": "HER2"}
    run = launch(
        alice,
        project,
        "iki/antibody.assemble",
        {"heavy_chain": heavy["id"], "light_chain": light["id"]},
        params,
    )
    effects = [e for e in run.get("effects") or [] if e["kind"] in ("created", "reused")]
    if [e["type"] for e in effects] != ["iki/antibody.Antibody"]:
        fail(f"{run['hrid']} made {effects}, not one iki/antibody.Antibody")
    antibody = record(alice, effects[0]["entity"])["values"]
    # Its chains are its parts, one copy of each for an Fv (ADR 0161), and its bonds each
    # variable domain's own disulfide, C22-C96 and C23-C88 (KEGG DRUG D03257's).
    wanted = [
        {"part": entry_of(heavy), "copies": 1, "role": "heavy"},
        {"part": entry_of(light), "copies": 1, "role": "light"},
    ]
    own = [(b["part"], b["residue"], b["partner_residue"]) for b in antibody.get("bonds") or []]
    if (antibody["components"], antibody["format"], own) != (
        wanted,
        "fv",
        [(entry_of(heavy), 22, 96), (entry_of(light), 23, 88)],
    ):
        fail(f"{effects[0]['hrid']} is {antibody}")
    ok(
        f"iki/antibody.assemble made {effects[0]['hrid']} of {heavy['hrid']} and {light['hrid']} "
        f"({run['hrid']})"
    )
    screened(alice, project, heavy, light)
    predicted(alice, project, effects[0]["entity"], heavy, light)
    complexes(alice, project, effects[0])
    biologics_ms(alice, project, effects[0]["entity"])


def biologics_ms(alice: requests.Session, project: dict[str, Any], antibody_id: str) -> None:
    """Map a published trastuzumab peptide onto the already registered Fv."""
    prefix = "iki/biologics_ms."
    antibody = record(alice, antibody_id)
    seen = matching_records(alice, project, prefix + "MSResult", "antibody", entry_of(antibody))
    if seen:
        comparison = seen[0]
    else:
        fixture = (
            Path(__file__).resolve().parents[2]
            / "plugins/biologics_ms/tests/data/trastuzumab-peptides.tsv"
        )
        report = upload(
            alice, project, fixture.name, fixture.read_bytes(), "text/tab-separated-values"
        )
        run = launch(
            alice,
            project,
            prefix + "characterize",
            {"antibody": antibody_id},
            {"analysis": "peptide_map", "report": report},
        )
        effect = next(e for e in run["effects"] if e["type"] == prefix + "MSResult")
        comparison = record(alice, effect["entity"])
    heavy = next(
        row for row in comparison["values"]["comparison"]["coverage"] if row["chain"] == "heavy"
    )
    if heavy["covered"] != 9:
        fail(f"{comparison['hrid']} contains {heavy}, expected nine covered heavy chain residues")
    if comparison["values"]["report_format"] != "iki_peptide_map_1":
        fail(f"{comparison['hrid']} did not record the peptide map 1 interchange profile")
    ok(f"{comparison['hrid']}: mapped the published trastuzumab peptide onto {antibody['hrid']}")


def complexes(alice: requests.Session, project: dict[str, Any], fv: dict[str, Any]) -> None:
    """Complexes (ADR 0161): the Fv's composition computed by the complex plugin's trigger;
    the Fv found by searching iki/core.Complex; and its chains, registered again as a
    protein complex, found to be the antibody already."""
    composed(alice, fv)
    fv_lot(alice, project, fv)
    found = api_call(
        alice,
        "POST",
        "/entities/search",
        {"type": "iki/core.Complex", "project": project["id"], "limit": 100},
    )
    if found.status_code != 200:
        fail(f"searching complexes answered HTTP {found.status_code}: {found.text[:300]}")
    hrids = {item["hrid"] for item in found.json()["items"]}
    if fv["hrid"] not in hrids:
        fail(f"searching iki/core.Complex found {sorted(hrids)}")
    ok(f"searching iki/core.Complex found {fv['hrid']}, an iki/antibody.Antibody")
    # The Fv's parts and copies, as a protein complex: one molecule is one entry.
    antibody = record(alice, fv["entity"])["values"]
    again = api_call(
        alice,
        "POST",
        "/entities",
        {
            "type": "iki/complex.ProteinComplex",
            "project": project["id"],
            "values": {"name": "Trastuzumab Fv", "components": antibody["components"]},
        },
    )
    problems = [] if again.status_code != 422 else again.json().get("errors") or []
    if not any("registered already, as a iki/antibody.Antibody" in p["detail"] for p in problems):
        fail(
            f"the Fv's chains as a protein complex answered {again.status_code}: {again.text[:300]}"
        )
    ok(f"the Fv's chains as a protein complex are {fv['hrid']}, registered already as an antibody")


def fv_lot(alice: requests.Session, project: dict[str, Any], fv: dict[str, Any]) -> None:
    """A protein lot of the Fv, a complex (ADR 0162): its concentration from its A280 and
    the Fv's composition, kept with each thiol free, with its two disulfides applied:
    49,850 + 2 x 125 L/(mol.cm) over 24,860.46 - 4 x 1.00794 g/mol, 2.0156 mL/(mg.cm)."""
    lot = created(
        alice,
        project,
        "iki/lots.ProteinLot",
        {"protein": fv["entity"], "a280": 0.85, "dilution_factor": 10, "path_length": 1},
        {"value": 5, "unit": "mg"},
    )
    run = launch(alice, project, "iki/lots.compute_protein_lot", {"lot": lot["id"]})
    got = record(alice, lot["id"])["values"]
    if (got.get("a280_extinction"), got.get("concentration")) != (2.0156, 4.2172):
        fail(f"{run['hrid']} worked out {lot['hrid']} as {got}, not 4.2172 mg/mL at 2.0156")
    ok(
        f"iki/lots.compute_protein_lot: {lot['hrid']}, a lot of {fv['hrid']}, is 4.2172 mg/mL "
        f"at the Fv's 2.0156 mL/(mg.cm), with its 2 disulfides ({run['hrid']})"
    )


def composed(alice: requests.Session, fv: dict[str, Any]) -> None:
    """The Fv's composition, which iki/complex.compute_composition's trigger computes when
    it's registered (ADR 0161)."""
    formula, weight, monoisotopic, chains, absorbance = FV_COMPOSITION
    deadline = time.monotonic() + 180
    got: dict[str, Any] = {}
    while time.monotonic() < deadline:
        got = record(alice, fv["entity"])["values"]
        if got.get("molecular_formula"):
            break
        time.sleep(2)
    if (
        got.get("molecular_formula"),
        got.get("molecular_weight"),
        got.get("monoisotopic_mass"),
        got.get("chain_count"),
        got.get("extinction_coefficient_reduced"),
    ) != (formula, weight, monoisotopic, chains, absorbance):
        fail(
            f"{fv['hrid']}'s composition is {got}, not {formula}, {weight} u, {chains} chains, "
            f"{absorbance} L/(mol.cm)"
        )
    ok(
        f"iki/complex.compute_composition's trigger computed {fv['hrid']}: {formula}, "
        f"{weight:,} u average, {monoisotopic:,} u monoisotopic, {chains} chains, "
        f"{absorbance:,} L/(mol.cm) at 280 nm"
    )


#: What iki/antibody.find_liabilities finds in trastuzumab's chains, on their IMGT numbering:
#: the NG in CDR-H2 and the DG in CDR-H3 that Lu et al. (2019) measured as hot spots (Kabat
#: H54 and H98), the DT in CDR-H1, the tryptophan and methionine in CDR-H3 that oxidise,
#: the E at residue 1 that cyclises, and the NT in CDR-L1. Its cysteines are the variable
#: domains' disulphides, so none is noncanonical, and neither chain carries a sequon.
TRASTUZUMAB_LIABILITIES: dict[str, list[tuple[str, str, int, str, str, str]]] = {
    "heavy": [
        ("pyroglutamate", "E", 1, "1", "framework", "low"),
        ("isomerisation", "DT", 31, "36", "cdr_h1", "medium"),
        ("deamidation", "NG", 55, "62", "cdr_h2", "high"),
        ("oxidation", "W", 99, "107", "cdr_h3", "medium"),
        ("isomerisation", "DG", 102, "110", "cdr_h3", "high"),
        ("oxidation", "M", 107, "115", "cdr_h3", "medium"),
    ],
    "light": [("deamidation", "NT", 30, "36", "cdr_l1", "medium")],
}


def screened(
    alice: requests.Session,
    project: dict[str, Any],
    heavy: dict[str, Any],
    light: dict[str, Any],
) -> None:
    """iki/antibody.find_liabilities: the sequence liabilities of trastuzumab's chains, read off
    the numbering ANARCI just gave them, on the backend tier (the plugin's own code)."""
    for side, chain in (("heavy", heavy), ("light", light)):
        run = launch(alice, project, "iki/antibody.find_liabilities", {"protein": chain["id"]})
        got = api_call(alice, "GET", f"/entities/{chain['id']}").json()["values"]
        wanted = TRASTUZUMAB_LIABILITIES[side]
        found = [
            (f["kind"], f["motif"], f["position"], f["number"], f["region"], f["severity"])
            for f in got.get("liabilities") or []
        ]
        screen = {
            "rule_set": "developability_v1",
            "numbering_scheme": "imgt",
            "cdr_definition": "imgt",
        }
        counts = Counter(kind for kind, *_ in wanted)
        if (
            found != wanted
            or got.get("liability_screen") != screen
            or got.get("liability_count") != len(wanted)
            or got.get("liability_counts") != dict(sorted(counts.items()))
        ):
            fail(f"{run['hrid']} screened {chain['hrid']} as {got.get('liabilities')}")
        ok(
            f"iki/antibody.find_liabilities screened {chain['hrid']} ({run['hrid']}): "
            f"{len(wanted)} {'liability' if len(wanted) == 1 else 'liabilities'}, "
            + ", ".join(
                f"{motif} at {number} in {region}" for _, motif, _, number, region, _ in wanted
            )
        )


def predicted(
    alice: requests.Session,
    project: dict[str, Any],
    antibody: str,
    heavy: dict[str, Any],
    light: dict[str, Any],
) -> None:
    """iki/ab_predict.predict_structure_with_immunebuilder, the optional structure provider:
    trastuzumab's structure from its chains, which its input includes (ADR 0105), in a pod of
    its own with that plugin's image. Registration, numbering and the screen ran without it."""
    # Numbering changed the chains: the run reads them as they are now.
    revisions = {
        c["id"]: api_call(alice, "GET", f"/entities/{c['id']}").json()["revision"]
        for c in (heavy, light)
    }
    # The first pulls the image, 2.7 GB, into the cluster.
    run = launch(
        alice,
        project,
        "iki/ab_predict.predict_structure_with_immunebuilder",
        {"antibody": antibody},
        within=1800,
    )
    (attempt,) = api_call(alice, "GET", f"/runs/{run['id']}/attempts").json()
    if attempt["environment_build"] != IMMUNEBUILDER_TOOLS.rpartition("@")[2]:
        fail(f"{run['hrid']} didn't run in the ImmuneBuilder structure-tools image: {attempt}")
    if attempt["template"] != "container-antibody-structures":
        fail(f"{run['hrid']} didn't run on the antibody structures template: {attempt}")
    read = {(i["field"], i["entity"], i["revision"]) for i in run.get("included") or []}
    wanted = {
        ("components.part", heavy["id"], revisions[heavy["id"]]),
        ("components.part", light["id"], revisions[light["id"]]),
    }
    if read != wanted:
        fail(f"{run['hrid']} read {read} through its input, not {wanted}")
    made = [e for e in run.get("effects") or [] if e["kind"] == "created"]
    if [e["type"] for e in made] != ["iki/protein.ProteinStructure"]:
        fail(f"{run['hrid']} made {made}, not one iki/protein.ProteinStructure")
    structure = record(alice, made[0]["entity"])["values"]
    error = structure.get("predicted_error")
    if (
        structure.get("method") != "predicted"
        or (structure.get("immunebuilder_model"), structure.get("immunebuilder_version"))
        != ("abodybuilder2", "1.2")
        or structure.get("chains") != [entry_of(heavy), entry_of(light)]
        or structure.get("chain_ids") != ["H", "L"]
        or not isinstance(error, (int, float))
        or not 0 < error < 2
        or not structure.get("structure")
    ):
        fail(f"{run['hrid']} made {made[0]['hrid']} as {structure}")
    ok(
        "iki/ab_predict.predict_structure_with_immunebuilder ran in a pod of its own "
        f"({run['hrid']}): "
        f"{made[0]['hrid']}, from {heavy['hrid']} and {light['hrid']} as numbered, "
        f"estimated error {error} Å (CDR-H3 {structure.get('cdr_h3_predicted_error')} Å)"
    )


def aligned_many(
    alice: requests.Session, project: dict[str, Any], chains: list[dict[str, Any]]
) -> None:
    """iki/protein.align_multiple: haemoglobin's chains and myoglobin through MAFFT, in a pod of
    its own with the plugin's image, into an alignment of the three, alpha its reference."""
    made = api_call(
        alice,
        "POST",
        "/entities",
        {
            "type": "iki/protein.Protein",
            "project": project["id"],
            "values": {
                "name": "MB_HUMAN",
                "sequence": MB,
                "organism": organism(alice, project, 9606, "Homo sapiens"),
            },
        },
    )
    if made.status_code not in (200, 201):
        fail(f"registering MB_HUMAN answered HTTP {made.status_code}: {made.text[:300]}")
    alpha, beta = chains
    myoglobin = made.json()
    action = "iki/protein.align_multiple"
    # A pod of its own pulls the image first.
    run = launch(
        alice,
        project,
        action,
        {"proteins": [beta["id"], myoglobin["id"]], "reference": alpha["id"]},
        within=900,
    )
    (attempt,) = api_call(alice, "GET", f"/runs/{run['id']}/attempts").json()
    if attempt["environment_build"] != ALIGNMENT_TOOLS.rpartition("@")[2]:
        fail(f"{run['hrid']} didn't run in the alignment-tools image: {attempt}")
    if attempt["template"] != "container-alignments":
        fail(f"{run['hrid']} didn't run on the alignments' template: {attempt}")
    created = [e for e in run.get("effects") or [] if e["kind"] == "created"]
    if [e["type"] for e in created] != ["iki/core.Alignment"]:
        fail(f"{run['hrid']} created {created}, not one iki/core.Alignment")
    values = record(alice, created[0]["entity"])["values"]
    if (
        values["members"] != [entry_of(alpha), entry_of(beta), entry_of(myoglobin)]
        or values["reference"] != entry_of(alpha)
        or not values["method"].startswith("MAFFT v7.505")
    ):
        fail(f"{created[0]['hrid']} is {values}")
    (kept,) = api_call(alice, "GET", f"/runs/{run['hrid']}/artifacts").json()
    stored = api_call(alice, "GET", f"/files/{values['aligned']}/content-url").json()
    if kept["path"] != "alignment.fasta" or stored["sha256"] != kept["sha256"]:
        fail(f"{created[0]['hrid']}'s file isn't the alignment.fasta {run['hrid']} kept")
    metrics = run.get("metrics") or {}
    if metrics.get("rows") != 3:
        fail(f"{run['hrid']} measured {metrics}, not three rows")
    ok(
        f"{action} ran MAFFT in a pod of its own ({run['hrid']}): {values['method']}, "
        f"{int(metrics['length'])} columns, making {created[0]['hrid']} of three globins"
    )


#: Residues a made-up structure's chains are made of.
RESIDUES = {"ALA": "A", "GLY": "G", "LYS": "K", "SER": "S", "TRP": "W", "VAL": "V"}


def upload(
    alice: requests.Session,
    project: dict[str, Any],
    name: str,
    data: bytes,
    media_type: str = "chemical/x-pdb",
    *,
    expected_scan: str = "clean",
) -> str:
    """A file added as the web app adds one: its parts sent to the URLs Iki signs, then
    checked. Returns its ID."""
    started = api_call(
        alice,
        "POST",
        "/uploads",
        {
            "project": project["id"],
            "name": name,
            "media_type": media_type,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    )
    if started.status_code != 201:
        fail(f"starting an upload answered HTTP {started.status_code}: {started.text[:300]}")
    made = started.json()
    offset = 0
    for part in made["parts"]:
        sent = requests.put(
            part["url"],
            data=data[offset : offset + part["size"]],
            allow_redirects=False,
            timeout=30,
        )
        if sent.status_code != 200:
            fail(f"sending part {part['number']} answered HTTP {sent.status_code}")
        offset += part["size"]
    api_call(alice, "POST", f"/uploads/{made['id']}/complete")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        made = api_call(alice, "GET", f"/uploads/{made['id']}").json()
        if made["state"] == "ready":
            scan = made["file"]["scan"]
            if scan["state"] != expected_scan or scan["usable"] != (expected_scan == "clean"):
                fail(f"unexpected scan decision for {name}: {scan}")
            return str(made["file"]["id"])
        if made["state"] not in ("checking", "scanning"):
            fail(f"the upload of {name} is {made['state']}")
        time.sleep(1)
    fail(f"the upload of {name} wasn't checked within a minute")


def _place(
    a: tuple[float, ...],
    b: tuple[float, ...],
    c: tuple[float, ...],
    bond: float,
    angle: float,
    torsion: float,
) -> tuple[float, ...]:
    """The atom bonded to c, `bond` from it, at `angle` b-c-d and dihedral `torsion` a-b-c-d
    (degrees)."""

    def sub(u: tuple[float, ...], v: tuple[float, ...]) -> tuple[float, ...]:
        return tuple(x - y for x, y in zip(u, v, strict=True))

    def unit(u: tuple[float, ...]) -> tuple[float, ...]:
        size = math.sqrt(sum(x * x for x in u))
        return tuple(x / size for x in u)

    def cross(u: tuple[float, ...], v: tuple[float, ...]) -> tuple[float, ...]:
        return (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])

    angle, torsion = math.radians(angle), math.radians(torsion)
    bc = unit(sub(c, b))
    n = unit(cross(sub(b, a), bc))
    m = cross(n, bc)
    d = (
        -bond * math.cos(angle),
        bond * math.sin(angle) * math.cos(torsion),
        bond * math.sin(angle) * math.sin(torsion),
    )
    return tuple(c[i] + bc[i] * d[0] + m[i] * d[1] + n[i] * d[2] for i in range(3))


def _helix(names: list[str], chain: str, shift: float, serial: int) -> list[str]:
    """An ideal alpha helix's backbone (N, CA, C, O) as PDB ATOM records, from Engh & Huber
    bond lengths and angles at phi -57.8, psi -47.0."""
    n: tuple[float, ...] = (shift, 0.0, 0.0)
    ca: tuple[float, ...] = (shift + 1.458, 0.0, 0.0)
    turn = math.radians(180 - 111.2)
    c: tuple[float, ...] = (ca[0] + 1.525 * math.cos(turn), 1.525 * math.sin(turn), 0.0)
    lines = []
    for number, name in enumerate(names, start=1):
        next_n = _place(n, ca, c, 1.329, 116.2, -47.0)
        o = _place(n, ca, c, 1.231, 120.5, -47.0 + 180)
        for atom, (x, y, z) in (("N", n), ("CA", ca), ("C", c), ("O", o)):
            serial += 1
            lines.append(
                f"ATOM  {serial:5d}  {atom:<3} {name} {chain}{number:4d}    "
                f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00 90.00           {atom[0]}"
            )
        next_ca = _place(ca, c, next_n, 1.458, 121.7, 180.0)
        n, ca, c = next_n, next_ca, _place(c, next_n, next_ca, 1.525, 111.2, -57.8)
    return lines


def structures(alice: requests.Session, project: dict[str, Any]) -> None:
    """A structure file imported into a structure linked to the proteins its chains are,
    made in one run (ADR 0064), with gemmi in the protein plugin's environment; then its
    secondary structure, by DSSP in the plugin's own image on the container tier."""
    # New residues each time, so each check registers a new structure. Two helices of
    # them, side by side.
    chain = [secrets.choice(list(RESIDUES)) for _ in range(12)]
    first = _helix(chain, "A", 0.0, 0)
    second = _helix(chain, "B", 30.0, len(first))
    data = ("\n".join([*first, "TER", *second, "TER", "END"]) + "\n").encode()
    file_id = upload(alice, project, "model_1.pdb", data)
    run = launch(alice, project, "iki/protein.import_structure", {}, {"file": file_id})
    made = {e["type"]: e for e in run["effects"]}
    if set(made) != {"iki/protein.Protein", "iki/protein.ProteinStructure"}:
        fail(f"{run['hrid']} (iki/protein.import_structure) made {run['effects']}")
    structure = api_call(
        alice, "GET", f"/entities/{made['iki/protein.ProteinStructure']['entity']}"
    ).json()
    values = structure["values"]
    protein = record(alice, made["iki/protein.Protein"]["entity"])
    chains = [entry_of(protein)] * 2
    if values.get("chains") != chains or values.get("structure") != file_id:
        fail(f"{run['hrid']} made {values}, not two chains of {protein['hrid']} in {file_id}")
    sequence = protein["values"]["sequence"]
    if sequence != "".join(RESIDUES[name] for name in chain):
        fail(f"{run['hrid']} read the chains' sequence as {sequence}")
    ok(
        f"iki/protein.import_structure made {structure['hrid']}, its chains A and B linked to "
        f"the protein it made with it ({run['hrid']})"
    )
    secondary(alice, project, structure)


def alphafold(alice: requests.Session, project: dict[str, Any]) -> None:
    """AlphaFold DB's model of crambin (UniProt P01542, 46 residues), fetched through the
    egress proxy, linked to its protein, its origin saying what made it, as its file
    says: AlphaFold 2.0 and the model's mean pLDDT, the source's word (ADR 0106)."""
    action = "iki/protein.fetch_structures_from_alphafold_db"
    run = launch(alice, project, action, {}, {"accessions": "P01542"}, within=300)
    if run["network"] != ["alphafold.ebi.ac.uk"]:
        fail(f"{run['hrid']} may reach {run['network']}, not AlphaFold DB alone")
    attempt = api_call(alice, "GET", f"/runs/{run['id']}/attempts").json()[-1]
    reached = {(e["host"], e["port"], e["refused"]) for e in attempt["network"]}
    if reached != {("alphafold.ebi.ac.uk", 443, None)}:
        fail(f"{run['hrid']} reached {reached}, not AlphaFold DB alone")
    made = {e["type"]: e for e in run["effects"] if e["kind"] in ("created", "reused")}
    # Crambin's organism, Crambe hispanica subsp. abyssinica (NCBI taxon 3721), is a record
    # its protein refers to (ADR 0140), made the first time an import meets it.
    expected = {"iki/protein.Protein", "iki/protein.ProteinStructure", "iki/core.Organism"}
    if set(made) != expected:
        fail(f"{run['hrid']} ({action}) made {run['effects']}")
    ref = made["iki/protein.ProteinStructure"]["entity"]
    structure = api_call(alice, "GET", f"/entities/{ref}").json()
    values = structure["values"]
    protein = entry_of(record(alice, made["iki/protein.Protein"]["entity"]))
    taxon = record(alice, made["iki/core.Organism"]["entity"])
    of_protein = record(alice, made["iki/protein.Protein"]["entity"])["values"].get("organism")
    if taxon["values"].get("taxon_id") != 3721 or of_protein != entry_of(taxon):
        fail(f"{run['hrid']} gave crambin the organism {of_protein}, not taxon 3721 ({taxon})")
    if (
        values.get("name") != "AF-P01542-F1"
        or values.get("method") != "predicted"
        or values.get("chains") != [protein]
        or not values.get("structure")
    ):
        fail(f"{run['hrid']} made {structure['hrid']} as {values}")
    origins = api_call(alice, "GET", f"/entities/{ref}/origins").json()
    stated = [o for o in origins if o["source"] == "AlphaFold DB" and o["id"] == "AF-P01542-F1"]
    made_by = stated[-1]["made"] if stated else None
    if (
        not made_by
        or made_by["kind"] != "computational"
        or made_by["software"] != [{"name": "AlphaFold", "version": "2.0"}]
        or not isinstance(made_by["details"].get("mean_plddt"), (int, float))
        or not stated[-1]["version"]
    ):
        fail(f"{structure['hrid']}'s origins are {origins}, not AlphaFold DB's model saying so")
    making = api_call(alice, "GET", f"/entities/{ref}/making").json()
    if not any(
        m["by"] == "source"
        and m["kind"] == "computational"
        and (m["software"], m["version"]) == ("AlphaFold", "2.0")
        and (m["source"] or {}).get("id") == "AF-P01542-F1"
        for m in making
    ) or any(m["by"] == "iki" for m in making):
        fail(f"{structure['hrid']}'s making is {making}, not AlphaFold DB's word alone")
    ok(
        f"{action} fetched {structure['hrid']} through the egress proxy ({run['hrid']}), "
        f"version {stated[-1]['version']}: made with AlphaFold 2.0, mean pLDDT "
        f"{made_by['details']['mean_plddt']}, as AlphaFold DB says"
    )


#: The protein plugin's structure-tools image (plugins/protein/environments/structure-tools.yaml).
STRUCTURE_TOOLS = (
    "ghcr.io/yogesh-dhande/iki-protein-structure-tools"
    "@sha256:28de99fb4916dd8380113f45588d63c6dc7b3f352589bf079237e817022895de"
)


def secondary(alice: requests.Session, project: dict[str, Any], structure: dict[str, Any]) -> None:
    """DSSP assigns each helix as one, in a pod of its own with the plugin's image, which
    Iki's publish workflow built (ADR 0065)."""
    action = "iki/protein.assign_secondary_structure"
    # A pod of its own pulls the image (about 1 GB) first.
    run = launch(alice, project, action, {"structure": structure["id"]}, within=900)
    (attempt,) = api_call(alice, "GET", f"/runs/{run['id']}/attempts").json()
    if attempt["environment_build"] != STRUCTURE_TOOLS.rpartition("@")[2]:
        fail(f"{run['hrid']} didn't run in the structure-tools image: {attempt}")
    if attempt["template"] != "container-structures" or not attempt["worker"].startswith(
        "iki-run-"
    ):
        fail(f"{run['hrid']} didn't run on a pod of its own: {attempt}")
    values = api_call(alice, "GET", f"/entities/{structure['id']}").json()["values"]
    assigned = values.get("secondary_structure") or []
    helix = values.get("helix_fraction")
    if (
        len(assigned) != 2
        or any(len(s) != 12 or "H" * 8 not in s for s in assigned)
        or not isinstance(helix, (int, float))
        or helix < 60
        or not values.get("dssp")
        or not values.get("accessible_surface_area")
    ):
        fail(f"{run['hrid']} ({action}) wrote {values}, not two helices")
    ok(
        f"{action} ran DSSP in {STRUCTURE_TOOLS.split('@')[0].rpartition('/')[2]} on a pod "
        f"of its own ({run['hrid']}): {' '.join(assigned)}, {helix}% helix, "
        f"{values['accessible_surface_area']} Å² accessible"
    )


#: The execution fixture's image (environments/tools.yaml): Python on Alpine,
#: so iki-worker, copied in, must be static.
TOOLS_IMAGE = (
    "docker.io/library/python:3.13-alpine"
    "@sha256:1e1f6334529d1fb26a2ef619d7cffb30a7c7d7cc1cc7338ebee283a58d1aeaca"
)


RUN_PODS = "/api/v1/namespaces/iki-runs/pods"


def _labelled_run_pod(version: str, worker: str, run: str, project: str) -> dict[str, Any]:
    """Read this worker's pod events since before launch, even if cleanup already deleted it."""
    wanted = {"iki.ikisci.org/run": run, "iki.ikisci.org/project": project}
    labels: dict[str, str] = {}
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        query = urlencode(
            {
                "watch": "true",
                "resourceVersion": version,
                "fieldSelector": f"metadata.name={worker}",
                "timeoutSeconds": 2,
            }
        )
        events = kubectl("get", "--raw", f"{RUN_PODS}?{query}", "--request-timeout=10s")
        for line in events.splitlines():
            event = json.loads(line)
            pod: dict[str, Any] = event["object"]
            if event["type"] == "ERROR":
                fail(f"watching {worker}: {pod.get('message', 'Kubernetes watch error')}")
            meta = pod["metadata"]
            version = meta["resourceVersion"]
            if meta.get("name") != worker:
                continue
            labels = meta.get("labels", {})
            if all(labels.get(k) == v for k, v in wanted.items()):
                return pod
            if event["type"] == "DELETED":
                fail(f"{worker} was deleted without its run and project labels: {labels}")
    fail(f"timed out waiting for {worker}'s run and project labels: {labels}")


def _sample(alice: requests.Session, project: dict[str, Any], **values: str) -> dict[str, Any]:
    return created(alice, project, "fixtures/worker_execution.ExecutionSample", values)


def _contained(
    alice: requests.Session, project: dict[str, Any], action: str, sample: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Runs `action` on `sample`: the run, its one attempt, and the storage it wrote."""
    launched = api_call(
        alice,
        "POST",
        f"/actions/{action}/runs",
        {"project": project["id"], "inputs": {"sample": sample["id"]}},
    )
    if launched.status_code != 201:
        fail(f"launching {action} answered HTTP {launched.status_code}: {launched.text[:300]}")
    # A pod of its own pulls the image first.
    run = followed(alice, launched.json(), within=300)
    if run["status"] != "succeeded":
        fail(f"{run['hrid']} ({action}) {run['status']}: {run['error']}")
    got = api_call(alice, "GET", f"/entities/{sample['id']}").json()["values"]
    (attempt,) = api_call(alice, "GET", f"/runs/{run['id']}/attempts").json()
    if attempt["environment_build"] != TOOLS_IMAGE.rpartition("@")[2]:
        fail(f"{run['hrid']} didn't record its image: {attempt}")
    if not attempt["worker"].startswith("iki-run-"):
        fail(f"{run['hrid']} ran on {attempt['worker']}, not a pod Iki started")
    return run, attempt, str(got.get("storage"))


def containers(alice: requests.Session, project: dict[str, Any]) -> None:
    """The execution fixture's actions on the container tier (ADR 0061, 0062): the provisioner
    starts a pod of its own in iki-runs for one, running the environment's image with
    iki-worker copied in; the other, persistent, runs one after another on a warm pod."""
    listed = {a["name"]: a for a in api_call(alice, "GET", "/actions").json()}
    for action in (
        "fixtures/worker_execution.assign_storage",
        "fixtures/worker_execution.tidy_storage",
    ):
        if not listed.get(action, {}).get("available"):
            fail(f"{action} isn't available to run: {listed.get(action)}")
    action = "fixtures/worker_execution.assign_storage"
    sample = _sample(alice, project)
    # A short run can finish and its pod disappear before followed() returns.
    version = json.loads(kubectl("get", "--raw", f"{RUN_PODS}?limit=1"))["metadata"][
        "resourceVersion"
    ]
    run, attempt, storage = _contained(alice, project, action, sample)
    if storage != "-80 °C freezer":
        fail(f"{run['hrid']} wrote {storage!r}, not the fixture's default storage")
    if attempt["template"] != "container-small":
        fail(f"{run['hrid']} didn't run on a pod of its own: {attempt}")
    pod = _labelled_run_pod(version, attempt["worker"], run["id"], project["id"])
    (container,) = pod["spec"]["containers"]
    if container["image"] != TOOLS_IMAGE or pod["spec"]["automountServiceAccountToken"]:
        fail(f"{attempt['worker']} isn't the environment's image without a Kubernetes token")
    ok(
        f"{action} ran in a pod of its own ({attempt['worker']}, {TOOLS_IMAGE.split('@')[0]}) "
        f"on the container tier ({run['hrid']}) and wrote storage {storage!r}"
    )
    ok(f"{attempt['worker']} is labelled with {run['hrid']}'s run and project")

    action = "fixtures/worker_execution.tidy_storage"
    # container-warm keeps one pod and allows no more (fixtures/compute.yaml), so both runs share
    # it only if it ran one after the other; what runs where doesn't depend on which of
    # two free pods won a lease.
    ran = [
        _contained(alice, project, action, _sample(alice, project, storage=" -80C   freezer"))
        for _ in range(2)
    ]
    workers = {attempt["worker"] for _, attempt, _ in ran}
    if len(workers) != 1 or any(a["template"] != "container-warm" for _, a, _ in ran):
        fail(f"{action}'s runs didn't share the one warm pod: {[a for _, a, _ in ran]}")
    if any(storage != "-80 °C freezer" for _, _, storage in ran):
        fail(f"{action} wrote {[s for _, _, s in ran]}")
    (worker,) = workers
    pod = json.loads(kubectl("get", "--raw", f"{RUN_PODS}/{worker}"))
    first = ran[0][0]
    started = datetime.fromisoformat(pod["metadata"]["creationTimestamp"])
    if started > datetime.fromisoformat(first["created_at"]):
        fail(f"{worker} started after {first['hrid']}: it wasn't kept warm")
    command = pod["spec"]["containers"][0]["command"]
    if "--once" in command:
        fail(f"{worker} runs one job only: {command}")
    ok(
        f"{action} ran twice ({', '.join(r['hrid'] for r, _, _ in ran)}) on the pod kept warm "
        f"({worker}), started before them, and tidied storage to '-80 °C freezer'"
    )

    # What they cost (ADR 0070): the warm pod's time holding each is that run's, recorded
    # when it let go, at container-warm's price.
    for done, _, _ in ran:
        cost = api_call(alice, "GET", f"/runs/{done['id']}").json()["cost"]
        if not cost or cost["currency"] != "USD" or not cost["final"] or cost["seconds"] <= 0:
            fail(f"{done['hrid']} doesn't say what it cost: {cost}")
    spent = api_call(alice, "GET", f"/projects/{project['id']}/costs").json()
    tidied = next((g for g in spent["groups"] if g["key"] == action), None)
    if tidied is None or tidied["runs"] < 2 or tidied["currency"] != "USD":
        fail(f"Atlas's costs don't count {action}'s runs: {spent}")
    ok(
        f"{action}'s runs say what they cost, and Atlas's costs count them "
        f"({tidied['runs']} runs, {tidied['seconds']:.0f} s of machine time)"
    )
    budget_warns(project)
    setup_builds(project)


def _snapshots(build: str) -> list[dict[str, Any]]:
    """The VolumeSnapshots of setup build `build`'s disks, in iki-runs."""
    found: list[dict[str, Any]] = json.loads(
        subprocess.run(  # noqa: S603 - fixed argv
            [  # noqa: S607
                "kubectl",
                "--context",
                CONTEXT,
                "-n",
                "iki-runs",
                "get",
                "volumesnapshots",
                "-l",
                f"iki.ikisci.org/setup={build}",
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )["items"]
    return found


def _on_shared_filesystem(path: str, mounted: str = "iki-shared") -> list[str]:
    """What's in `path` under the dev cluster's shared file system's (or, with `mounted`
    `iki-bucket`, the bucket's) directory for Iki (deploy/dev/manifests/), on the kind node;
    empty if it's gone."""
    listed = subprocess.run(  # noqa: S603 - fixed argv
        [  # noqa: S607
            "docker",
            "exec",
            "iki-control-plane",
            "find",
            f"/var/local/{mounted}/iki/{path}",
            "-mindepth",
            "1",
            "-printf",
            "%P %u\n",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return sorted(listed.stdout.split("\n")[:-1]) if listed.returncode == 0 else []


def _until_ended(session: requests.Session, ref: str) -> dict[str, Any]:
    deadline = time.monotonic() + 300
    while True:
        build: dict[str, Any] = api_call(session, "GET", ref).json()
        if build["state"] in ("succeeded", "failed", "timed_out", "cancelled"):
            return build
        if time.monotonic() > deadline:
            fail(f"{build['hrid']} is still {build['state']} after five minutes")
        time.sleep(3)


def setup_builds(project: dict[str, Any]) -> None:
    """An environment's reference data (ADRs 0075, 0077, 0078): bob, a Compute admin, asks
    for a setup build of the execution fixture's `reference` environment, which runs in three
    places; alice, another, approves each. In iki-runs its script fills a disk, which is
    snapshotted; in iki-runs-shared, its own directory on a shared file system; in
    iki-runs-bucket, a disk that's then copied into its own directory in a bucket. A run of
    decode_storage reads a copy of the snapshot and records the version; with that
    retired, the next reads the shared file system's, and with that retired, the bucket's.
    Retired, each version goes. Both are made Compute admins for it, then put back as they
    were."""
    bob, alice = sign_in_as("bob"), sign_in_as("alice")
    restore = []
    for session in (bob, alice):
        me = api_call(session, "GET", "/me").json()
        path = f"/users/{me['id']}/org-roles"
        restore.append((path, me["org_roles"]))
        granted = api_call(bob, "PUT", path, {"roles": [*me["org_roles"], "compute_admin"]})
        if granted.status_code != 200:
            fail(f"{me['email']} couldn't be made a Compute Admin")
    try:
        environment = "fixtures/worker_execution.reference"
        (listed,) = [
            e
            for e in api_call(bob, "GET", "/compute/volumes").json()
            if e["environment"] == environment
        ]
        if [v["name"] for v in listed["volumes"]] != ["storage-codes"]:
            fail(f"{environment}'s volumes aren't listed: {listed}")
        made = api_call(
            bob, "POST", "/compute/setups", {"environment": environment, "reason": "e2e"}
        )
        if made.status_code != 201:
            fail(f"asking for a setup build answered HTTP {made.status_code}: {made.text[:300]}")
        builds = {b["place"]: b for b in made.json()}
        if sorted(builds) != ["iki-runs", "iki-runs-bucket", "iki-runs-shared"]:
            fail(f"asking for a setup build made builds in {sorted(builds)}")
        for build in builds.values():
            ref = f"/compute/setups/{build['hrid']}"
            if build["state"] != "awaiting_approval":
                fail(f"{build['hrid']} didn't wait for approval: {build['state']}")
            if api_call(bob, "POST", f"{ref}/approve").status_code != 409:
                fail(f"bob approved the setup build he asked for, {build['hrid']}")
            if api_call(alice, "POST", f"{ref}/approve").status_code != 200:
                fail(f"alice couldn't approve {build['hrid']}")
        for place, build in builds.items():
            ref = f"/compute/setups/{build['hrid']}"
            build = builds[place] = _until_ended(bob, ref)
            log = api_call(bob, "GET", f"{ref}/log").json()["text"]
            if build["state"] != "succeeded":
                fail(f"{build['hrid']} {build['state']}: {build['error']}\n{log[-2000:]}")
            if build["data_version"] != build["hrid"] or "bytes of storage codes" not in log:
                said = build["data_version"]
                fail(f"{build['hrid']} didn't say what it wrote: {said!r}, {log!r}")
        snapshots, shared = builds["iki-runs"], builds["iki-runs-shared"]
        bucket = builds["iki-runs-bucket"]
        (snapshot,) = _snapshots(snapshots["id"])
        if not snapshot.get("status", {}).get("readyToUse"):
            fail(f"{snapshots['hrid']}'s snapshot isn't ready: {snapshot.get('status')}")
        ok(
            f"{snapshots['hrid']}, asked for by bob and approved by alice, filled {environment}'s "
            f"volume ({snapshots['data_version']}) and left snapshot "
            f"{snapshot['metadata']['name']}"
        )
        (volume,) = shared["volumes"]
        directory = volume["handle"]
        if volume["kind"] != "directory" or volume["state"] != "ready":
            fail(f"{shared['hrid']}'s version isn't a ready directory: {volume}")
        # Its worker made the version's own directory, as runs' user, and the script filled it.
        if _on_shared_filesystem(directory) != ["codes.json 10001"]:
            fail(f"{shared['hrid']} left {_on_shared_filesystem(directory)} in {directory}")
        ok(f"{shared['hrid']} filled its own directory on the shared file system, {directory}")
        (copied,) = bucket["volumes"]
        prefix = copied["handle"]
        if copied["kind"] != "bucket" or copied["state"] != "ready":
            fail(f"{bucket['hrid']}'s version isn't ready in the bucket: {copied}")
        # A Job copied the disk its script filled into the version's own directory, as
        # runs' user.
        if _on_shared_filesystem(prefix, "iki-bucket") != ["codes.json 10001"]:
            fail(f"{bucket['hrid']} left {_on_shared_filesystem(prefix, 'iki-bucket')} in {prefix}")
        ok(f"{bucket['hrid']} filled a disk, which a Job copied into the bucket, {prefix}")

        sample = _sample(alice, project, storage="-80")
        run, attempt, storage = _contained(
            alice, project, "fixtures/worker_execution.decode_storage", sample
        )
        read = (attempt.get("volumes") or {}).get("storage-codes") or {}
        if (
            storage != f"-80 °C freezer ({snapshots['hrid']})"
            or read.get("hrid") != snapshots["hrid"]
        ):
            fail(f"{run['hrid']} wrote {storage!r} from {attempt.get('volumes')}")
        ok(
            f"{run['hrid']} read a copy of version {read['version']} ({read['data_version']}) "
            f"and wrote {storage!r}"
        )

        if api_call(bob, "POST", f"/compute/setups/{snapshots['hrid']}/retire").status_code != 200:
            fail(f"bob couldn't retire {snapshots['hrid']}")
        deadline = time.monotonic() + 180
        while _snapshots(snapshots["id"]):
            if time.monotonic() > deadline:
                fail(f"{snapshots['hrid']}'s snapshot outlived its retirement")
            time.sleep(3)
        ok(f"retired, {snapshots['hrid']}'s snapshot was deleted once no pod had a copy")

        # With no version in iki-runs, runs go where one is ready, and mount it read-only.
        sample = _sample(alice, project, storage="ln2")
        run, attempt, storage = _contained(
            alice, project, "fixtures/worker_execution.decode_storage", sample
        )
        read = (attempt.get("volumes") or {}).get("storage-codes") or {}
        if (
            storage != f"Liquid nitrogen dewar ({shared['hrid']})"
            or read.get("hrid") != shared["hrid"]
        ):
            fail(f"{run['hrid']} wrote {storage!r} from {attempt.get('volumes')}")
        ok(f"{run['hrid']} read {shared['hrid']}'s directory on the shared file system")

        ref = f"/compute/setups/{shared['hrid']}"
        if api_call(bob, "POST", f"{ref}/retire").status_code != 200:
            fail(f"bob couldn't retire {shared['hrid']}")
        deadline = time.monotonic() + 180
        while api_call(bob, "GET", ref).json()["volumes"][0]["state"] != "deleted":
            if time.monotonic() > deadline:
                fail(f"{shared['hrid']}'s directory outlived its retirement")
            time.sleep(3)
        if _on_shared_filesystem(directory):
            fail(f"{shared['hrid']} says its directory was removed, but {directory} is there")
        ok(f"retired, {shared['hrid']}'s directory was removed once no pod had it mounted")

        # And then to the bucket.
        sample = _sample(alice, project, storage="4")
        run, attempt, storage = _contained(
            alice, project, "fixtures/worker_execution.decode_storage", sample
        )
        read = (attempt.get("volumes") or {}).get("storage-codes") or {}
        if storage != f"4 °C fridge ({bucket['hrid']})" or read.get("hrid") != bucket["hrid"]:
            fail(f"{run['hrid']} wrote {storage!r} from {attempt.get('volumes')}")
        ok(f"{run['hrid']} read {bucket['hrid']}'s directory in the bucket")
        ref = f"/compute/setups/{bucket['hrid']}"
        if api_call(bob, "POST", f"{ref}/retire").status_code != 200:
            fail(f"bob couldn't retire {bucket['hrid']}")
        deadline = time.monotonic() + 180
        while api_call(bob, "GET", ref).json()["volumes"][0]["state"] != "deleted":
            if time.monotonic() > deadline:
                fail(f"{bucket['hrid']}'s directory outlived its retirement")
            time.sleep(3)
        if _on_shared_filesystem(prefix, "iki-bucket"):
            fail(f"{bucket['hrid']} says its directory was removed, but {prefix} is there")
        ok(f"retired, {bucket['hrid']}'s directory was removed from the bucket")
    finally:
        for path, roles in restore:
            api_call(bob, "PUT", path, {"roles": roles})


def budget_warns(project: dict[str, Any]) -> None:
    """A budget's warning reaches its Compute Admins (ADR 0071): the task runners check
    budgets every minute. Bob is made a Compute Admin for it, then put back as he was."""
    bob = sign_in_as("bob")
    me = api_call(bob, "GET", "/me").json()
    roles = me["org_roles"]
    path = f"/users/{me['id']}/org-roles"
    if api_call(bob, "PUT", path, {"roles": [*roles, "compute_admin"]}).status_code != 200:
        fail("bob couldn't be made a Compute Admin")
    made = api_call(
        bob,
        "POST",
        "/compute/budgets",
        {"project": project["id"], "amount": "0.01", "warn_at": [1]},
    )
    try:
        if made.status_code != 201:
            fail(f"setting a budget answered HTTP {made.status_code}: {made.text[:300]}")
        deadline = time.monotonic() + 120
        while True:
            inbox = api_call(bob, "GET", "/notifications?limit=50").json()["items"]
            told = [
                n
                for n in inbox
                if n["kind"] == "budget.threshold_reached"
                and project["name"] in n["title"]
                and n["created_at"] >= made.json()["created_at"]
            ]
            if told:
                break
            if time.monotonic() > deadline:
                fail(f"no budget warning reached bob: {made.json()}")
            time.sleep(3)
        ok(f"a budget for Atlas warned its Compute Admin: {told[0]['title']!r}")
    finally:
        if made.status_code == 201:
            api_call(bob, "DELETE", f"/compute/budgets/{made.json()['id']}")
        api_call(bob, "PUT", path, {"roles": roles})


def product_scenarios(alice: requests.Session, project: dict[str, Any]) -> None:
    """Run the scientific checks for the product lock in the newly created Atlas project."""
    dna_actions(alice, project)
    reads_aligned(alice, project)
    sequences_aligned(alice, project)
    sequence_library_annotation(alice, project)
    proteins(alice, project)
    compounds(alice, project)
    oligonucleotides(alice, project)
    conjugates(alice, project)
    polymers(alice, project)
    plasmids(alice, project)
    lots(alice, project)
    mouse_stocks(alice, project)
    bookings(alice, project)
    signatures(alice, project)
    stability_study(alice, project)
    plate_assay(alice, project)
    reagent_stock(alice, project)
    dose_response_tutorial(alice, project)
    qc_release(alice, project)
    sequencing(alice, project)
    sequencing_analysis(alice, project)
    proteomics(alice, project)
    whole_plasmid(alice, project)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="run the sign-in and product checks")
    parser.add_argument(
        "--fixtures", action="store_true", help="also run checks that require the dev-ci overlay"
    )
    parser.add_argument("--admin", default="bob", help="Keycloak user who finishes setup")
    parser.add_argument("--iki-port", type=int, default=8000)
    parser.add_argument("--keycloak-port", type=int, default=8080)
    parser.add_argument("--mailpit-port", type=int, default=8025)
    parser.add_argument("--webhook-port", type=int, default=8090)
    parser.add_argument("--lims-port", type=int, default=8091)
    parser.add_argument("--foldcloud-port", type=int, default=8092)
    parser.add_argument(
        "--web", default=WEB["target"], help="web pod or deployment for exec and logs"
    )
    args = parser.parse_args()
    if args.fixtures and not args.check:
        parser.error("--fixtures requires --check")
    WEB["target"] = args.web
    MAILPIT["url"] = f"http://127.0.0.1:{args.mailpit_port}"
    RECEIVER["url"] = f"http://127.0.0.1:{args.webhook_port}"
    LIMS["url"] = f"http://127.0.0.1:{args.lims_port}"
    FOLDCLOUD_REQUESTS["url"] = f"http://127.0.0.1:{args.foldcloud_port}/requests"
    route(args.iki_port, args.keycloak_port)
    admin = set_up(args.admin)
    if args.check:
        project = check("alice", admin, (args.iki_port, args.keycloak_port))
        provisioning(admin)
        rest_api(admin)
        webhooks(admin)
        certify_dev_signatures(admin)
        alice = sign_in_as("alice")
        product_scenarios(alice, project)
        sys.stdout.write(
            kubectl(
                "exec",
                "-i",
                WEB["target"],
                "-c",
                "web",
                "--",
                "env",
                f"IKI_SEED_PROJECT={project['id']}",
                "iki-server",
                "shell",
                stdin=Path(__file__).with_name("check_seed.py").read_text(),
            )
        )
        if args.fixtures:
            lims(alice, project)
            foldcloud(alice, project)
            containers(alice, project)
            plugin_export(alice, project)
        oauth(admin)
        saml()


if __name__ == "__main__":
    main()
