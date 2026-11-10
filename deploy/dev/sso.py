"""Sets up sign-in on the dev cluster through the real wizard and Keycloak.

    uv run python deploy/dev/sso.py           # set up (or re-set up) SSO, bob becomes admin
    uv run python deploy/dev/sso.py --check   # same, then end-to-end checks: sign-in as
                                              # alice (also in Chromium), her
                                              # notification and its email, SCIM
                                              # provisioning, the API, OAuth, and SAML

It drives Iki like a browser: opens a one-time setup link from
`iki-server setup_link`, fills in the wizard, signs in on Keycloak's login
page, and finishes setup. Connections to Iki (localhost:8000) and Keycloak
(keycloak.iki.localhost:8080) go to the given local ports, so it works with
Tilt's port-forwards or with e2e.sh's own.
"""

import argparse
import base64
import hashlib
import html
import http.cookiejar
import json
import re
import secrets
import subprocess
import sys
import time
from html.parser import HTMLParser
from typing import Any, NoReturn
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
import urllib3.util.connection

IKI = "http://localhost:8000"
#: The web workload to exec into and read logs from (``--web``).
WEB = {"target": "deploy/iki-web"}
ISSUER = "http://keycloak.iki.localhost:8080/realms/iki"
SAML_METADATA = f"{ISSUER}/protocol/saml/descriptor"
#: Mailpit's API (``--mailpit-port``), where the dev cluster's email lands.
MAILPIT = {"url": "http://127.0.0.1:8025"}
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

    teams = api_call(member, "GET", "/teams?can_create_projects=true&limit=100").json()["items"]
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
    api_call(admin, "PATCH", f"/service-accounts/{account['id']}", {"active": False})
    if bearer_call(issued.json()["access_token"], "GET", "/me").status_code != 401:
        fail("a deactivated service account's token still works")
    ok("a service account got a token with client credentials; deactivating ended it")


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="also run end-to-end sign-in checks")
    parser.add_argument("--admin", default="bob", help="Keycloak user who finishes setup")
    parser.add_argument("--iki-port", type=int, default=8000)
    parser.add_argument("--keycloak-port", type=int, default=8080)
    parser.add_argument("--mailpit-port", type=int, default=8025)
    parser.add_argument(
        "--web", default=WEB["target"], help="web pod or deployment for exec and logs"
    )
    args = parser.parse_args()
    WEB["target"] = args.web
    MAILPIT["url"] = f"http://127.0.0.1:{args.mailpit_port}"
    route(args.iki_port, args.keycloak_port)
    admin = set_up(args.admin)
    if args.check:
        check("alice", admin, (args.iki_port, args.keycloak_port))
        provisioning(admin)
        rest_api(admin)
        oauth(admin)
        saml()


if __name__ == "__main__":
    main()
