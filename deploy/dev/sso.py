"""Sets up sign-in on the dev cluster through the real wizard and Keycloak.

    uv run python deploy/dev/sso.py           # set up (or re-set up) SSO, bob becomes admin
    uv run python deploy/dev/sso.py --check   # same, then end-to-end checks: sign-in as
                                              # alice (also in Chromium), her
                                              # notification and its email,
                                              # Keycloak's own email, SCIM
                                              # provisioning, the API, webhooks,
                                              # the DNA plugin's actions on the
                                              # cluster's worker, a model behind
                                              # an outside API (the fake
                                              # FoldCloud), OAuth, and SAML

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
from typing import Any, NoReturn
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

import requests
import urllib3.util.connection

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
CONTEXT = "kind-iki"
NAMESPACE = "iki"


def fail(message: str) -> NoReturn:
    sys.stderr.write(f"\033[1;31mFAIL\033[0m {message}\n")
    sys.exit(1)


def ok(message: str) -> None:
    sys.stdout.write(f"\033[1;32m  ok\033[0m {message}\n")


def kubectl(*args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["kubectl", "--context", CONTEXT, "-n", NAMESPACE, *args],  # noqa: S607
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


def teams_and_projects(admin: requests.Session, member: requests.Session, email: str) -> None:
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
    project = created.json()
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
        timeout=10,
    )
    if token.status_code != 200:
        fail(f"Keycloak's admin token: HTTP {token.status_code} {token.text[:200]}")
    admin = {"Authorization": f"Bearer {token.json()['access_token']}"}
    found = requests.get(
        f"{keycloak}/admin/realms/iki/users",
        params={"username": user, "exact": "true"},
        headers=admin,
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
        response = self.session.request(method, f"{IKI}/scim/v2{path}", json=body)
        if response.status_code >= 400:
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
    ok("the Org Admin turned on SCIM; bob's and alice's accounts are now provisioned")

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
        method, f"{IKI}/api/v1{path}", json=body, headers={"Authorization": f"Bearer {token}"}
    )


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
    started = app.post(
        f"{IKI}/oauth/device-authorization/", data={"client_id": "iki-cli", "scope": "read write"}
    )
    if started.status_code != 200:
        fail(f"device authorization answered HTTP {started.status_code}: {started.text}")
    device = started.json()
    alice = sign_in_as("alice")
    entered = page_post(
        alice, f"{IKI}/oauth/device/", {"user_code": device["user_code"]}, allow_redirects=False
    )
    if entered.status_code != 302:
        fail(f"entering the device code answered HTTP {entered.status_code}")
    confirm = urljoin(IKI, entered.headers["Location"])
    if "Iki CLI" not in alice.get(confirm).text:
        fail("the device confirmation page doesn't name the CLI")
    done = page_post(alice, confirm, {"granted": ["read"], "access": "all"})
    if done.status_code != 200:
        fail(f"approving the device answered HTTP {done.status_code}")
    time.sleep(device["interval"])
    tokens = app.post(
        f"{IKI}/oauth/token/",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device["device_code"],
            "client_id": "iki-cli",
        },
    )
    if tokens.status_code != 200 or tokens.json().get("scope") != "read":
        fail(f"the device poll answered HTTP {tokens.status_code}: {tokens.text}")
    token = tokens.json()["access_token"]
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
        fail("the new app isn't on Settings → Apps")
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


def check(user: str, admin: requests.Session, ports: tuple[int, int]) -> None:
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

    teams_and_projects(admin, session, f"{user}@iki.test")
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


def actions() -> None:
    """The DNA plugin's actions, run by the cluster's backend worker in its built environment."""
    alice = sign_in_as("alice")
    mine = api_call(alice, "GET", "/projects?member=true&limit=100").json()["items"]
    project = next(p for p in mine if p["name"] == "Atlas")
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
    reads_aligned(alice, project)
    sequences_aligned(alice, project)
    proteins(alice, project)
    compounds(alice, project)
    oligonucleotides(alice, project)
    plasmids(alice, project)
    lots(alice, project)


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
    created = [e for e in run.get("effects") or [] if e["kind"] == "created"]
    if [e["type"] for e in created] != ["iki/core.Alignment"]:
        fail(f"{run['hrid']} created {created}, not one iki/core.Alignment")
    values = api_call(alice, "GET", f"/entities/{created[0]['hrid']}").json()["values"]
    plasmid_entry = entry_of(reference)
    if values["reference"] != plasmid_entry or values["members"] != [plasmid_entry]:
        fail(f"{created[0]['hrid']} is {values}")
    # It keeps Iki's alignment, its file, and the same with `-` for every gap, which tools
    # reading aligned FASTA take (alignment-standard.fasta).
    artifacts = api_call(alice, "GET", f"/runs/{run['hrid']}/artifacts").json()
    kept = {a["name"]: a for a in artifacts}
    if set(kept) != {"alignment.fasta", "alignment-standard.fasta"}:
        fail(f"{run['hrid']} kept {sorted(kept)}, not its alignment and the standard one")
    stored = api_call(alice, "GET", f"/files/{values['aligned']}/content-url").json()
    if stored["sha256"] != kept["alignment.fasta"]["sha256"]:
        fail(f"{created[0]['hrid']}'s file isn't the alignment.fasta {run['hrid']} kept")
    ok(
        f"{action} aligned a trace and a read across the origin of a plasmid ({run['hrid']}): "
        f"{metrics['coverage_percent']}% covered, {expected[:-1].split(': ', 1)[1]}, "
        f"making {created[0]['hrid']}"
    )


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


#: The fake LIMS's records (deploy/dev/manifests/fake-lims.yaml).
LIMS_RECORDS = ("LIMS-0001", "LIMS-0002", "LIMS-0003")


def lims() -> None:
    """The example LIMS plugin against the fake LIMS: an import that reaches it over HTTPS,
    through the egress proxy, at the host the deployment's settings give, then again,
    matching each record to its protein, and a write-back."""
    alice = sign_in_as("alice")
    mine = api_call(alice, "GET", "/projects?member=true&limit=100").json()["items"]
    project = next(p for p in mine if p["name"] == "Atlas")
    action = "iki/example_lims.import_from_lims"
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
    sent = launch(alice, project, "iki/example_lims.send_to_lims", {"proteins": [first["id"]]})
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
    ok(f"iki/example_lims.send_to_lims wrote {first['hrid']} to {LIMS_RECORDS[0]} ({sent['hrid']})")
    bookings(alice, project)
    signatures(alice, project, first)


def signing(
    session: requests.Session,
    sample: dict[str, Any],
    state: str,
    as_user: str,
    *,
    someone_else: bool = False,
    reason: str = "",
) -> Any:  # noqa: ANN401
    """Starts signing for a state, signs in again at Keycloak as `as_user` (someone other
    than the session's person, if `someone_else`), and returns what came of it."""
    etag = api_call(session, "GET", f"/entities/{sample['id']}").headers["ETag"]
    started = api_call(
        session,
        "POST",
        f"/entities/{sample['id']}/signatures",
        {
            "field": "status",
            "state": state,
            "acknowledge": True,
            "reason": reason,
            "return_to": f"/entities/{sample['hrid']}/",
        },
        {"If-Match": etag},
    )
    if started.status_code != 201:
        fail(f"signing {sample['hrid']} answered HTTP {started.status_code}: {started.text[:300]}")
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
        fail(f"signing in again came back to {response.url}, not the sample's page")
    return api_call(session, "GET", f"/signings/{signing_id}").json()


def signatures(alice: requests.Session, project: dict[str, Any], protein: dict[str, Any]) -> None:
    """Electronic signatures (ADR 0126) against Keycloak: submitting a sample signs in again
    there (prompt=login, its auth_time checked, ADR 0124), sets its state, shows with it and
    verifies; signing in at Keycloak as someone else gives no signature."""
    bob = sign_in_as("bob")
    if api_call(bob, "GET", "/signatures/certification").json()["certification"] is None:
        certified = api_call(
            bob,
            "POST",
            "/signatures/certification",
            {"submitted_on": datetime.now(UTC).date().isoformat(), "reference": "DEV-CERT-1"},
        )
        if certified.status_code != 200:
            fail(f"recording the certification answered HTTP {certified.status_code}")
    members = api_call(alice, "GET", f"/projects/{project['id']}/members?limit=100").json()
    grant = next(m for m in members["items"] if (m["user"] or {}).get("email") == "alice@iki.test")
    roles = {
        "roles": ["iki/example_lims.analyst", "iki/example_lims.reviewer", "iki/example_lims.qa"]
    }
    given = api_call(
        alice, "PUT", f"/projects/{project['id']}/members/{grant['id']}/lab-roles", roles
    )
    if given.status_code != 200:
        fail(f"giving alice lab roles answered HTTP {given.status_code}: {given.text[:300]}")
    made = api_call(
        alice,
        "POST",
        "/entities",
        {
            "type": "iki/example_lims.Sample",
            "project": project["id"],
            # Its guards submit it for review once its result is recorded (ADR 0128).
            "values": {"protein": entry_of(protein), "lot": "DEV-1", "result": "pass"},
        },
    )
    if made.status_code != 201:
        fail(f"making a sample answered HTTP {made.status_code}: {made.text[:300]}")
    sample = made.json()
    for state in ("received", "in_testing"):
        etag = api_call(alice, "GET", f"/entities/{sample['id']}").headers["ETag"]
        moved = api_call(
            alice,
            "POST",
            f"/entities/{sample['id']}/states/status",
            {"state": state, "reason": ""},
            {"If-Match": etag},
        )
        if moved.status_code != 200:
            fail(f"setting {sample['hrid']} {state} answered HTTP {moved.status_code}")
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
    # Rejecting it, which alice may (approving it is someone else's: she submitted it).
    refused = signing(
        alice, sample, "rejected", "bob", someone_else=True, reason="Contaminated in transit"
    )
    if refused["status"] != "failed" or "Someone else" not in refused["message"]:
        fail(f"signing in at Keycloak as bob to sign as alice gave {refused}")
    ok(f"signing in at Keycloak as bob gave no signature of alice's: {refused['message']}")


#: Where the booking calendar's places get their bookings (ADR 0117).
CALENDAR = "/components/iki/example_lims.booking_calendar/entities"


def bookings(alice: requests.Session, project: dict[str, Any]) -> None:
    """The example LIMS plugin's booking calendar (ADR 0117): each of its places gets the
    bookings its search finds, computed by the server as the person may read them, with
    only the fields it reads; a refinement only narrows; an app can place it; and someone
    outside the project gets nothing."""

    def made(kind: str, values: dict[str, Any]) -> dict[str, Any]:
        body = {"type": f"iki/example_lims.{kind}", "project": project["id"], "values": values}
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
    if set(items[0]["values"]) != {"purpose", "instrument", "starts_at", "ends_at"}:
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
        "when it narrows, and Alice's on her home, with only the four fields it reads"
    )
    # An app places it beside Iki's own grid, given the instrument picked there.
    spec = {
        "version": 1,
        "panels": [
            {
                "id": "instruments",
                "component": "iki.entities",
                "props": {"type": "iki/example_lims.Instrument", "sort": "name"},
            },
            {
                "id": "calendar",
                "title": "Bookings",
                "component": "iki/example_lims.booking_calendar",
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
        [i["hrid"] for i in shown(bob, {"home": True}).json().get("items", [])],
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
    foldcloud(alice, project, protein)
    chains = aligned(alice, project)
    reads_on_protein(alice, project, chains[1])
    aligned_many(alice, project, chains)
    globins_app(alice, project)
    dashboard(alice, project, relatives_app(alice, project))
    app_draft(alice, project)
    structures(alice, project)
    alphafold(alice, project)
    antibodies(alice, project)


#: The example models plugin's outside API (plugins/example_models), and where the dev
#: cluster's settings point it: the fake FoldCloud (manifests/fake-foldcloud.yaml).
FOLDCLOUD = "iki/example_models.foldcloud"
FOLDCLOUD_HOST = "fake-foldcloud.iki.svc"
#: What deploy/dev/compute.yaml charges for its calls, in USD: per call, and per GPU second
#: the finished job reports. The fake reports half a GPU second per residue.
FOLDCLOUD_PER_CALL = 0.01
FOLDCLOUD_PER_GPU_SECOND = 0.0011


def foldcloud(alice: requests.Session, project: dict[str, Any], protein: dict[str, Any]) -> None:
    """A model behind an outside API (ADR 0130): refused until an Org Admin approves
    FoldCloud for Atlas, then a prediction that calls it through the egress proxy, which
    adds the organization's key, parks on its job without a worker, wakes on its signed
    webhook and resumes to import the structure. The proxy's record of each call says what
    answered, and their prices are the run's cost. The approval is revoked afterwards."""
    bob = sign_in_as("bob")
    approvals = f"/outside-apis/{FOLDCLOUD}/approvals"
    # One left by an earlier check that stopped part way.
    api_call(bob, "DELETE", f"{approvals}/{project['id']}")
    action = "iki/example_models.predict_structure"
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
    finally:
        api_call(bob, "DELETE", f"{approvals}/{project['id']}")


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


def plasmids(alice: requests.Session, project: dict[str, Any]) -> None:
    """The plasmid plugin, in its environment with Biopython: pUC19 registered from the
    2,686 bases of L09137.2 and annotated by its trigger from the library of common vector
    parts, at the coordinates pUC19's published map gives; the same molecule read from
    another origin on the other strand finding the entries it has already; a plasmid under
    a material transfer agreement having to name it (ADR 0127); and a cloning plan that
    puts pUC19's own polylinker back between its EcoRI and HindIII sites making pUC19
    again — the same molecule, so the registry gives back the entry it has (ADR 0138) —
    which its construct states then follow, up to the four-eyes guard that won't let
    whoever built it verify it (ADR 0128)."""
    members = api_call(alice, "GET", f"/projects/{project['id']}/members?limit=100").json()
    grant = next(m for m in members["items"] if (m["user"] or {}).get("email") == "alice@iki.test")
    held = {held["role"] for held in grant.get("lab_roles") or []}
    roles = sorted(held | {"iki/plasmid.cloner", "iki/plasmid.construct_reviewer"})
    given = api_call(
        alice, "PUT", f"/projects/{project['id']}/members/{grant['id']}/lab-roles", {"roles": roles}
    )
    if given.status_code != 200:
        fail(f"giving alice the plasmid plugin's lab roles answered HTTP {given.status_code}")
    # A trigger is off until a project Admin turns it on, and then its runs are launched as
    # them: nothing annotates a plasmid, or checks a plan against the clone's sequence,
    # before alice turns these on herself.
    annotating = "iki/plasmid.annotate_new_plasmid"
    for trigger in (annotating, "iki/plasmid.check_stated_product"):
        on = api_call(alice, "POST", f"/projects/{project['id']}/triggers/{trigger}/enable", {})
        if on.status_code != 200 or not on.json().get("enabled"):
            fail(f"turning {trigger} on answered HTTP {on.status_code}: {on.text[:300]}")
    action = "iki/plasmid.register_plasmid"
    run = launch(
        alice, project, action, {}, {"bases": PUC19, "name": "pUC19", "accession": "L09137.2"}
    )
    kinds = {e["type"]: e["kind"] for e in run["effects"]}
    if kinds.get("iki/plasmid.Plasmid") != "created":
        fail(f"{run['hrid']} ({action}) made {run['effects']}, not a plasmid")
    plasmid = record(
        alice, next(e["entity"] for e in run["effects"] if e["type"].endswith("Plasmid"))
    )
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
    plan = assembled(alice, project, plasmid)
    constructs(alice, project, plan)


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


def assembled(
    alice: requests.Session, project: dict[str, Any], plasmid: dict[str, Any]
) -> dict[str, Any]:
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
    return assembly


def constructs(alice: requests.Session, project: dict[str, Any], assembly: dict[str, Any]) -> None:
    """A construct's states: Checked only once the plan matches its product, Built from
    Checked, and Verified by someone other than whoever built it (ADR 0128)."""
    for state in ("checked", "built"):
        etag = api_call(alice, "GET", f"/entities/{assembly['id']}").headers["ETag"]
        moved = api_call(
            alice,
            "POST",
            f"/entities/{assembly['id']}/states/status",
            {"state": state, "reason": ""},
            {"If-Match": etag},
        )
        if moved.status_code != 200:
            fail(
                f"setting {assembly['hrid']} {state} answered HTTP {moved.status_code}: "
                f"{moved.text[:300]}"
            )
    etag = api_call(alice, "GET", f"/entities/{assembly['id']}").headers["ETag"]
    refused = api_call(
        alice,
        "POST",
        f"/entities/{assembly['id']}/states/status",
        {"state": "verified", "reason": ""},
        {"If-Match": etag},
    )
    if refused.status_code != 409 or refused.json().get("code") != "guard_refused":
        fail(
            f"verifying her own construct answered HTTP {refused.status_code}: {refused.text[:300]}"
        )
    ok(
        f"{assembly['hrid']} is Checked and Built, and alice may not verify what she built: "
        f"{refused.json()['detail']}"
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
    # 50 uL of a 100 uM solution is 5 nmol.
    drawn = api_call(
        alice,
        "POST",
        f"/entities/{lot['id']}/amount/draw",
        {"amount": {"value": 50, "unit": "uL"}, "reason": "Dosed"},
    )
    if drawn.status_code != 200:
        fail(f"drawing from {lot['hrid']} answered HTTP {drawn.status_code}: {drawn.text[:300]}")
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
        counts = Counter(kind for kind, *_ in wanted)
        if (
            found != wanted
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
    """iki/antibody.predict_structure_with_immunebuilder: trastuzumab's structure from its chains,
    which its input includes (ADR 0105), in a pod of its own with the plugin's image."""
    # Numbering changed the chains: the run reads them as they are now.
    revisions = {
        c["id"]: api_call(alice, "GET", f"/entities/{c['id']}").json()["revision"]
        for c in (heavy, light)
    }
    # The first pulls the image, 2.7 GB, into the cluster.
    run = launch(
        alice,
        project,
        "iki/antibody.predict_structure_with_immunebuilder",
        {"antibody": antibody},
        within=1800,
    )
    (attempt,) = api_call(alice, "GET", f"/runs/{run['id']}/attempts").json()
    if attempt["environment_build"] != IMMUNEBUILDER_TOOLS.rpartition("@")[2]:
        fail(f"{run['hrid']} didn't run in the antibody structure-tools image: {attempt}")
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
        or structure.get("chains") != [entry_of(heavy), entry_of(light)]
        or structure.get("chain_ids") != ["H", "L"]
        or not isinstance(error, (int, float))
        or not 0 < error < 2
        or not structure.get("structure")
    ):
        fail(f"{run['hrid']} made {made[0]['hrid']} as {structure}")
    ok(
        "iki/antibody.predict_structure_with_immunebuilder ran in a pod of its own "
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
        sent = requests.put(part["url"], data=data[offset : offset + part["size"]], timeout=30)
        if sent.status_code != 200:
            fail(f"sending part {part['number']} answered HTTP {sent.status_code}")
        offset += part["size"]
    api_call(alice, "POST", f"/uploads/{made['id']}/complete")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        made = api_call(alice, "GET", f"/uploads/{made['id']}").json()
        if made["state"] == "ready":
            return str(made["file"]["id"])
        if made["state"] != "checking":
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


#: The plugin template's image (plugin-template/environments/tools.yaml): Python on Alpine,
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
    made = api_call(
        alice,
        "POST",
        "/entities",
        {
            "type": "acme/example.Sample",
            "project": project["id"],
            "values": {"name": f"e2e-{secrets.token_hex(4)}", "kind": "dna", **values},
        },
    )
    if made.status_code not in (200, 201):
        fail(f"registering a sample answered HTTP {made.status_code}: {made.text[:300]}")
    sample: dict[str, Any] = made.json()
    return sample


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


def containers() -> None:
    """The plugin template's actions on the container tier (ADR 0061, 0062): the provisioner
    starts a pod of its own in iki-runs for one, running the environment's image with
    iki-worker copied in; the other, persistent, runs one after another on a warm pod."""
    alice = sign_in_as("alice")
    mine = api_call(alice, "GET", "/projects?member=true&limit=100").json()["items"]
    project = next(p for p in mine if p["name"] == "Atlas")
    listed = {a["name"]: a for a in api_call(alice, "GET", "/actions").json()}
    for action in ("acme/example.assign_storage", "acme/example.tidy_storage"):
        if not listed.get(action, {}).get("available"):
            fail(f"{action} isn't available to run: {listed.get(action)}")
    action = "acme/example.assign_storage"
    sample = _sample(alice, project)
    # A short run can finish and its pod disappear before followed() returns.
    version = json.loads(kubectl("get", "--raw", f"{RUN_PODS}?limit=1"))["metadata"][
        "resourceVersion"
    ]
    run, attempt, storage = _contained(alice, project, action, sample)
    if storage != "-80 °C freezer":
        fail(f"{run['hrid']} wrote {storage!r}, not the plugin's default storage")
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

    action = "acme/example.tidy_storage"
    # container-warm keeps one pod and allows no more (compute.yaml), so both runs share
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
    for a setup build of the plugin template's `reference` environment, which runs in three
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
        environment = "acme/example.reference"
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
            if (
                build["data_version"] != "Storage codes 2026-11"
                or "bytes of storage codes" not in log
            ):
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
        run, attempt, storage = _contained(alice, project, "acme/example.decode_storage", sample)
        read = (attempt.get("volumes") or {}).get("storage-codes") or {}
        if storage != "-80 °C freezer" or read.get("hrid") != snapshots["hrid"]:
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
        run, attempt, storage = _contained(alice, project, "acme/example.decode_storage", sample)
        read = (attempt.get("volumes") or {}).get("storage-codes") or {}
        if storage != "Liquid nitrogen dewar" or read.get("hrid") != shared["hrid"]:
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
        run, attempt, storage = _contained(alice, project, "acme/example.decode_storage", sample)
        read = (attempt.get("volumes") or {}).get("storage-codes") or {}
        if storage != "4 °C fridge" or read.get("hrid") != bucket["hrid"]:
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="also run end-to-end sign-in checks")
    parser.add_argument("--admin", default="bob", help="Keycloak user who finishes setup")
    parser.add_argument("--iki-port", type=int, default=8000)
    parser.add_argument("--keycloak-port", type=int, default=8080)
    parser.add_argument("--mailpit-port", type=int, default=8025)
    parser.add_argument("--webhook-port", type=int, default=8090)
    parser.add_argument("--lims-port", type=int, default=8091)
    parser.add_argument(
        "--web", default=WEB["target"], help="web pod or deployment for exec and logs"
    )
    args = parser.parse_args()
    WEB["target"] = args.web
    MAILPIT["url"] = f"http://127.0.0.1:{args.mailpit_port}"
    RECEIVER["url"] = f"http://127.0.0.1:{args.webhook_port}"
    LIMS["url"] = f"http://127.0.0.1:{args.lims_port}"
    route(args.iki_port, args.keycloak_port)
    admin = set_up(args.admin)
    if args.check:
        check("alice", admin, (args.iki_port, args.keycloak_port))
        provisioning(admin)
        rest_api(admin)
        webhooks(admin)
        actions()
        lims()
        containers()
        oauth(admin)
        saml()


if __name__ == "__main__":
    main()
