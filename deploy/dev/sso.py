"""Sets up sign-in on the dev cluster through the real wizard and Keycloak.

    uv run python deploy/dev/sso.py           # set up (or re-set up) SSO, bob becomes admin
    uv run python deploy/dev/sso.py --check   # same, then end-to-end sign-in checks as alice

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
    """On Keycloak's login page, signs in and follows the redirects back to Iki."""
    if not response.url.startswith(ISSUER):
        fail(f"expected Keycloak's login page, got {response.url}")
    action, inputs = form(response, "kc-form-login")
    password = secret("keycloak-dev", "IKI_DEV_USER_PASSWORD")
    return session.post(action, data={**inputs, "username": username, "password": password})


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


def set_up(admin: str) -> requests.Session:
    """Runs the wizard; returns the admin's signed-in browser."""
    session = browser()
    response = submit(session, f"{IKI}/setup/", {"token": setup_link()}, form_id="setup-token-form")
    if urlsplit(response.url).path == "/setup/organization/":
        response = submit(session, response.url, {"organization_name": "Iki (development)"})
    if urlsplit(response.url).path != "/setup/identity-provider/":
        fail(f"wizard went to {response.url} after the setup link (HTTP {response.status_code})")
    response = submit(
        session,
        response.url,
        {
            "display_name": "Keycloak",
            "issuer": ISSUER,
            "client_id": "iki-web",
            "client_secret": secret("keycloak-dev", "IKI_DEV_OIDC_CLIENT_SECRET"),
        },
    )
    if urlsplit(response.url).path != "/setup/verify/":
        errors = re.findall(r'class="field-error"[^>]*>([^<]+)', response.text)
        fail(f"identity provider step failed: {errors or response.status_code}")
    ok("wizard checked Keycloak's discovery document from inside the cluster")
    response = keycloak_sign_in(session, submit(session, response.url), admin)
    if urlsplit(response.url).path != "/setup/complete/":
        fail(f"after signing in at Keycloak, landed on {response.url}")
    response = submit(session, response.url)
    if urlsplit(response.url).path != "/setup/done/" or "Iki is ready" not in response.text:
        fail(f"finishing setup landed on {response.url}")
    ok(f"setup finished through Keycloak; {admin} is an Org Admin")
    return session


def post(session: requests.Session, url: str, data: dict[str, str]) -> requests.Response:
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
    expected = {"setup_completed", "signed_in", "signed_out", "team_created", "project_created"}
    if not expected <= names:
        fail(f"expected sign-in events in the web logs, found {sorted(n for n in names if n)}")
    if any("code=" in line or "state=" in line for line in logs.splitlines()):
        fail("OAuth code or state leaked into the web logs")
    ok("setup, sign-in, changes and sign-out are logged; no OAuth codes in the logs")


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


if __name__ == "__main__":
    main()
