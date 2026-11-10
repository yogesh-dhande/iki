"""Sets up sign-in on the dev cluster through the real wizard and Keycloak.

    uv run python deploy/dev/sso.py           # set up (or re-set up) SSO, bob becomes admin
    uv run python deploy/dev/sso.py --check   # same, then end-to-end checks: sign-in as
                                              # alice, SCIM provisioning, and SAML

It drives Iki like a browser: opens a one-time setup link from
`iki-server setup_link`, fills in the wizard, signs in on Keycloak's login
page, and finishes setup. Connections to Iki (localhost:8000) and Keycloak
(keycloak.iki.localhost:8080) go to the given local ports, so it works with
Tilt's port-forwards or with e2e.sh's own.
"""

import argparse
import base64
import html
import http.cookiejar
import json
import re
import subprocess
import sys
import time
from html.parser import HTMLParser
from typing import Any, NoReturn
from urllib.parse import urljoin, urlsplit

import requests
import urllib3.util.connection

IKI = "http://localhost:8000"
#: The web workload to exec into and read logs from (``--web``).
WEB = {"target": "deploy/iki-web"}
ISSUER = "http://keycloak.iki.localhost:8080/realms/iki"
SAML_METADATA = f"{ISSUER}/protocol/saml/descriptor"
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

    page = member.get(f"{IKI}/projects/new/")
    team_id = re.search(rf'<option value="([0-9a-f-]+)"[^>]*>{re.escape(name)}</option>', page.text)
    if not team_id:
        fail("the new team isn't offered on the new project page")
    response = post(member, f"{IKI}/projects/new/", {"team": team_id.group(1), "name": "Atlas"})
    project_path = urlsplit(response.url).path
    if "You’re its Admin" not in response.text:
        fail(f"creating a project landed on {response.url} (HTTP {response.status_code})")
    ok("the team member created a project and became its Admin")

    page = admin.get(f"{IKI}{project_path}")
    if "You’re not a member of this project" not in page.text:
        fail("the Org Admin can read a project they're not a member of")
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


def sign_in_as(user: str) -> requests.Session:
    session = browser()
    action, inputs = form(session.get(f"{IKI}/login/"))
    response = keycloak_sign_in(
        session, session.post(action, data=inputs, headers={"Origin": IKI}), user
    )
    if urlsplit(response.url).path != "/" or f"Welcome, {user.title()}" not in response.text:
        fail(f"signing in as {user} landed on {response.url} (HTTP {response.status_code})")
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


def saml() -> None:
    """Switches sign-in to Keycloak over SAML through recovery, signs in, and switches back."""
    set_up("bob", protocol="saml")
    sign_in_as("alice")
    ok("alice signed in through Keycloak over SAML (signed assertion, SP-initiated)")
    set_up("bob")


def check(user: str, admin: requests.Session) -> None:
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
    if urlsplit(response.url).path != "/" or f"Welcome, {user.title()}" not in response.text:
        fail(f"signing in as {user} landed on {response.url} (HTTP {response.status_code})")
    cookie = next((c for c in session.cookies if c.name == "iki_session"), None)
    if cookie is None or not cookie.has_nonstandard_attr("HttpOnly") or cookie.expires is not None:
        fail("session cookie must be HttpOnly and end with the browser session")
    ok(f"{user} signed in through Keycloak with PKCE; session cookie is HttpOnly")

    teams_and_projects(admin, session, f"{user}@iki.test")

    action, inputs = form(session.get(f"{IKI}/"))
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
    parser.add_argument(
        "--web", default=WEB["target"], help="web pod or deployment for exec and logs"
    )
    args = parser.parse_args()
    WEB["target"] = args.web
    route(args.iki_port, args.keycloak_port)
    admin = set_up(args.admin)
    if args.check:
        check("alice", admin)
        provisioning(admin)
        rest_api(admin)
        saml()


if __name__ == "__main__":
    main()
