"""Sets up sign-in on the dev cluster through the real wizard and Keycloak.

    uv run python deploy/dev/sso.py           # set up (or re-set up) SSO, bob becomes admin
    uv run python deploy/dev/sso.py --check   # same, then end-to-end checks: sign-in as
                                              # alice (also in Chromium), her
                                              # notification and its email, SCIM
                                              # provisioning, the API, webhooks,
                                              # the DNA plugin's actions on the
                                              # cluster's worker, OAuth, and SAML

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
import subprocess
import sys
import time
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, NoReturn
from urllib.parse import parse_qs, quote, urljoin, urlsplit

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
        response = submit(session, response.url, {"organization_name": "Iki (development)"})
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

    people = admin.get(f"{IKI}/people/")
    me = re.search(r'href="(/people/[0-9a-f-]+/)">Bob', people.text)
    if not me:
        fail("bob isn't on the people page")
    post(admin, f"{IKI}{me.group(1)}", {"roles": ["org_admin"]})  # as on a first run
    if admin.get(f"{IKI}/audit/").status_code != 403:
        fail("an Org Admin without the Auditor role can read the audit log")
    post(admin, f"{IKI}{me.group(1)}", {"roles": ["org_admin", "auditor"]})
    log = admin.get(f"{IKI}/audit/")
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
    page = post(admin, f"{IKI}/settings/sign-in/", {"action": "create_token"})
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
        admin, f"{IKI}/settings/sign-in/groups/{group['id']}/", {"action": "link", "team": "new"}
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
) -> requests.Response:
    """Calls /api/v1 like Iki's pages do: the session, plus the page's CSRF token."""
    headers = {"Origin": IKI}
    if method != "GET":
        page = session.get(f"{IKI}/projects/")
        token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)
        if not token:
            fail("pages don't carry the CSRF token for API calls")
        headers["X-CSRFToken"] = token.group(1)
    return session.request(method, f"{IKI}/api/v1{path}", json=body, headers=headers)


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
    log = admin.get(f"{IKI}/audit/")
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
    apps = admin.get(f"{IKI}/settings/apps/")
    link = re.search(r'href="(/settings/apps/\d+/)">' + re.escape(client["client_name"]), apps.text)
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
        f"{IKI}/service-accounts/{account['id']}/",
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
        f"{IKI}/service-accounts/{account['id']}/",
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
    for name in ("dna.compute_composition", "dna.primer_properties"):
        if not listed.get(name, {}).get("available"):
            fail(f"{name} isn't available to run: {listed.get(name)}")
    # New bases each time, so each check registers new entities.
    bases = "".join(secrets.choice("ACGT") for _ in range(40))
    primer = bases[:20]
    expected = {
        "dna.compute_composition": ("dna.DNASequence", "sequence", bases, {"length": 40}),
        "dna.primer_properties": ("dna.Primer", "primer", primer, {"length": 20}),
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
        if action == "dna.primer_properties" and not isinstance(
            got.get("melting_temperature"), (int, float)
        ):
            fail(f"{run['hrid']} wrote no melting temperature: {got}")
        ok(f"{action} ran on the cluster's backend worker ({run['hrid']}) and wrote {got}")
    proteins(alice, project)


#: The fake LIMS's records (deploy/dev/manifests/fake-lims.yaml).
LIMS_RECORDS = ("LIMS-0001", "LIMS-0002", "LIMS-0003")


def lims() -> None:
    """The example LIMS plugin against the fake LIMS: an import that reaches it over HTTPS,
    through the egress proxy, at the host the deployment's settings give, then again,
    matching each record to its protein, and a write-back."""
    alice = sign_in_as("alice")
    mine = api_call(alice, "GET", "/projects?member=true&limit=100").json()["items"]
    project = next(p for p in mine if p["name"] == "Atlas")
    action = "example_lims.import_from_lims"
    run = launch(alice, project, action, {})
    if run["network"] != ["fake-lims.iki.svc"]:
        fail(f"{run['hrid']} may reach {run['network']}, not the host lims_host gives")
    attempt = api_call(alice, "GET", f"/runs/{run['id']}/attempts").json()[-1]
    reached = [(e["host"], e["port"], e["refused"]) for e in attempt["network"]]
    if reached != [("fake-lims.iki.svc", 443, None)]:
        fail(f"{run['hrid']} reached {reached}, not the fake LIMS alone")
    if sorted(e["type"] for e in run["effects"]) != ["protein.Protein"] * 3:
        fail(f"{run['hrid']} wrote {run['effects']}, not the LIMS's three proteins")
    found = {}
    for record in LIMS_RECORDS:
        query = f"type=protein.Protein&field=lims_id&value={record}&project={project['id']}"
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
    sent = launch(alice, project, "example_lims.send_to_lims", {"proteins": [first["id"]]})
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
    ok(f"example_lims.send_to_lims wrote {first['hrid']} to {LIMS_RECORDS[0]} ({sent['hrid']})")


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
        {"type": "dna.DNASequence", "project": project["id"], "values": {"bases": bases}},
    )
    if made.status_code not in (200, 201):
        fail(f"registering a DNA sequence answered HTTP {made.status_code}: {made.text[:300]}")
    dna = made.json()
    run = launch(alice, project, "protein.translate", {"sequence": dna["id"]})
    created = [e for e in run["effects"] if e["kind"] in ("created", "reused")]
    if [e["type"] for e in created] != ["protein.Protein"]:
        fail(f"{run['hrid']} (protein.translate) made {run['effects']}, not one protein")
    protein = api_call(alice, "GET", f"/entities/{created[0]['entity']}").json()
    values = protein["values"]
    if len(values.get("sequence", "")) != 30 or values.get("encoded_by") != dna["id"]:
        fail(f"{run['hrid']} made {values}, not 30 residues encoded by {dna['hrid']}")
    ok(f"protein.translate made {protein['hrid']} from {dna['hrid']} ({run['hrid']})")
    run = launch(alice, project, "protein.compute_properties", {"protein": protein["id"]})
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
        f"protein.compute_properties ran in Biopython's environment ({run['hrid']}): "
        f"{weight} Da, pI {point}"
    )
    aligned(alice, project)
    structures(alice, project)


#: Human haemoglobin's alpha and beta chains (UniProt P69905, P68871).
HBA = (
    "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHFDLSHGSAQVKGHGKKVADALTNAVAHVDDMPNALSALSDLHAH"
    "KLRVDPVNFKLLSHCLLVTLAAHLPAEFTPAVHASLDKFLASVSTVLTSKYR"
)
HBB = (
    "MVHLTPEEKSAVTALWGKVNVDEVGGEALGRLLVVYPWTQRFFESFGDLSTPDAVMGNPKVKAHGKKVLGAFSDGLAHLDNLKGTF"
    "ATLSELHCDKLHVDPENFRLLGNVLVCVLAHHFGKEFTPPVQAAYQKVVAGVANALAHKYH"
)


def aligned(alice: requests.Session, project: dict[str, Any]) -> None:
    """protein.align_pair on haemoglobin's chains: its metrics on the run, and its alignment
    kept as artifacts (ADR 0068), downloaded from where Iki's URL sends a browser."""
    chains = []
    for name, sequence in (("HBA_HUMAN", HBA), ("HBB_HUMAN", HBB)):
        made = api_call(
            alice,
            "POST",
            "/entities",
            {
                "type": "protein.Protein",
                "project": project["id"],
                "values": {"name": name, "sequence": sequence},
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
    launched = api_call(alice, "POST", "/actions/protein.align_pair/runs", body)
    if launched.status_code != 201:
        fail(f"launching protein.align_pair answered HTTP {launched.status_code}")
    run = followed(alice, launched.json(), within=120)
    if run["status"] != "succeeded":
        fail(f"{run['hrid']} (protein.align_pair) {run['status']}: {run['error']}")
    metrics = run.get("metrics") or {}
    if metrics.get("identity_percent") != 43.6 or metrics.get("length") != 149:
        fail(f"{run['hrid']} measured {metrics}, not 43.6% identity over 149 columns")
    kept = api_call(alice, "GET", f"/runs/{run['hrid']}/artifacts").json()
    if sorted((k["path"], k["state"]) for k in kept) != [
        ("alignment.fasta", "kept"),
        ("alignment.txt", "kept"),
    ]:
        fail(f"{run['hrid']} kept {kept}, not its alignment")
    text = next(k for k in kept if k["path"] == "alignment.txt")
    got = api_call(alice, "GET", f"/runs/{run['hrid']}/artifacts/{text['id']}/content")
    if got.status_code != 200 or hashlib.sha256(got.content).hexdigest() != text["sha256"]:
        fail(f"downloading {run['hrid']}'s alignment.txt answered HTTP {got.status_code}")
    if b"# Identity:   65/149 (43.6%)" not in got.content:
        fail(f"{run['hrid']}'s alignment.txt says {got.content[:400]!r}")
    ok(
        f"protein.align_pair aligned haemoglobin's chains ({run['hrid']}): "
        f"{metrics['identity_percent']}% identical, kept {len(kept)} artifacts "
        f"until {text['expires_at'][:10]}"
    )


#: Residues a made-up structure's chains are made of.
RESIDUES = {"ALA": "A", "GLY": "G", "LYS": "K", "SER": "S", "TRP": "W", "VAL": "V"}


def upload(alice: requests.Session, project: dict[str, Any], name: str, data: bytes) -> str:
    """A file added as the web app adds one: its parts sent to the URLs Iki signs, then
    checked. Returns its ID."""
    started = api_call(
        alice,
        "POST",
        "/uploads",
        {
            "project": project["id"],
            "name": name,
            "media_type": "chemical/x-pdb",
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
    run = launch(alice, project, "protein.import_structure", {}, {"file": file_id})
    made = {e["type"]: e for e in run["effects"]}
    if set(made) != {"protein.Protein", "protein.ProteinStructure"}:
        fail(f"{run['hrid']} (protein.import_structure) made {run['effects']}")
    structure = api_call(
        alice, "GET", f"/entities/{made['protein.ProteinStructure']['entity']}"
    ).json()
    values = structure["values"]
    protein = made["protein.Protein"]["entity"]
    if values.get("chains") != [protein, protein] or values.get("structure") != file_id:
        fail(f"{run['hrid']} made {values}, not two chains of {protein} in {file_id}")
    sequence = api_call(alice, "GET", f"/entities/{protein}").json()["values"]["sequence"]
    if sequence != "".join(RESIDUES[name] for name in chain):
        fail(f"{run['hrid']} read the chains' sequence as {sequence}")
    ok(
        f"protein.import_structure made {structure['hrid']}, its chains A and B linked to "
        f"the protein it made with it ({run['hrid']})"
    )
    secondary(alice, project, structure)


#: The protein plugin's structure-tools image (plugins/protein/environments/structure-tools.yaml).
STRUCTURE_TOOLS = (
    "ghcr.io/yogesh-dhande/iki-protein-structure-tools"
    "@sha256:28de99fb4916dd8380113f45588d63c6dc7b3f352589bf079237e817022895de"
)


def secondary(alice: requests.Session, project: dict[str, Any], structure: dict[str, Any]) -> None:
    """DSSP assigns each helix as one, in a pod of its own with the plugin's image, which
    Iki's publish workflow built (ADR 0065)."""
    action = "protein.assign_secondary_structure"
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


def _run_pods() -> list[dict[str, Any]]:
    pods: list[dict[str, Any]] = json.loads(
        subprocess.run(  # noqa: S603 - fixed argv
            ["kubectl", "--context", CONTEXT, "-n", "iki-runs", "get", "pods", "-o", "json"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )["items"]
    return pods


def _sample(alice: requests.Session, project: dict[str, Any], **values: str) -> dict[str, Any]:
    made = api_call(
        alice,
        "POST",
        "/entities",
        {
            "type": "example.Sample",
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
    for action in ("example.assign_storage", "example.tidy_storage"):
        if not listed.get(action, {}).get("available"):
            fail(f"{action} isn't available to run: {listed.get(action)}")
    action = "example.assign_storage"
    run, attempt, storage = _contained(alice, project, action, _sample(alice, project))
    if storage != "-80 °C freezer":
        fail(f"{run['hrid']} wrote {storage!r}, not the plugin's default storage")
    if attempt["template"] != "container-small":
        fail(f"{run['hrid']} didn't run on a pod of its own: {attempt}")
    pod = next((p for p in _run_pods() if p["metadata"]["name"] == attempt["worker"]), None)
    if pod is None:
        fail(f"no pod {attempt['worker']} in iki-runs")
    (container,) = pod["spec"]["containers"]
    if container["image"] != TOOLS_IMAGE or pod["spec"]["automountServiceAccountToken"]:
        fail(f"{attempt['worker']} isn't the environment's image without a Kubernetes token")
    ok(
        f"{action} ran in a pod of its own ({attempt['worker']}, {TOOLS_IMAGE.split('@')[0]}) "
        f"on the container tier ({run['hrid']}) and wrote storage {storage!r}"
    )
    # Its pod says whose it is, for the cluster's own cost tools (ADR 0070).
    wanted = {"iki.ikisci.org/run": run["id"], "iki.ikisci.org/project": project["id"]}
    deadline = time.monotonic() + 60
    while True:
        pod = next((p for p in _run_pods() if p["metadata"]["name"] == attempt["worker"]), None)
        labels = (pod or {}).get("metadata", {}).get("labels", {})
        if all(labels.get(k) == v for k, v in wanted.items()):
            break
        if pod is None or time.monotonic() > deadline:
            fail(f"{attempt['worker']} isn't labelled with its run and project: {labels}")
        time.sleep(2)
    ok(f"{attempt['worker']} is labelled with {run['hrid']}'s run and project")

    action = "example.tidy_storage"
    ran = [
        _contained(alice, project, action, _sample(alice, project, storage=" -80C   freezer"))
        for _ in range(2)
    ]
    workers = {attempt["worker"] for _, attempt, _ in ran}
    if len(workers) != 1 or any(a["template"] != "container-warm" for _, a, _ in ran):
        fail(f"{action}'s runs didn't share the warm pod: {[a for _, a, _ in ran]}")
    if any(storage != "-80 °C freezer" for _, _, storage in ran):
        fail(f"{action} wrote {[s for _, _, s in ran]}")
    (worker,) = workers
    pod = next((p for p in _run_pods() if p["metadata"]["name"] == worker), None)
    if pod is None:
        fail(f"no warm pod {worker} in iki-runs")
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
        environment = "example.reference"
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
        run, attempt, storage = _contained(alice, project, "example.decode_storage", sample)
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
        run, attempt, storage = _contained(alice, project, "example.decode_storage", sample)
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
        run, attempt, storage = _contained(alice, project, "example.decode_storage", sample)
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
