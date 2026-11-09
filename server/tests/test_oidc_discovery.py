"""Checking an identity provider's discovery document before it is saved."""

import http.server
import socket
import threading
from collections.abc import Iterator

import pytest
from fake_idp import FakeIdP

from iki_server.accounts.oidc import DiscoveryError, discover


@pytest.fixture
def idp(fake_idp: FakeIdP) -> FakeIdP:
    return fake_idp


@pytest.mark.django_db
def test_valid_provider(idp: FakeIdP) -> None:
    metadata = discover(idp.issuer, allow_http=True)
    assert metadata.authorization_endpoint == f"{idp.issuer}/authorize"
    assert metadata.token_auth_method == "client_secret_basic"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"issuer": "https://other.example"}, "reports its issuer as"),
        ({"token_endpoint": None}, "no token_endpoint"),
        ({"jwks_uri": "ftp://x"}, r"jwks uri must be an http\(s\) URL"),
        ({"response_types_supported": ["id_token"]}, "authorization code flow"),
        ({"code_challenge_methods_supported": ["plain"]}, "PKCE with S256"),
        ({"token_endpoint_auth_methods_supported": ["private_key_jwt"]}, "client secrets"),
    ],
)
def test_unusable_providers_are_explained(
    idp: FakeIdP, override: dict[str, object], message: str
) -> None:
    idp.discovery_overrides = override
    with pytest.raises(DiscoveryError, match=message):
        discover(idp.issuer, allow_http=True)


@pytest.mark.django_db
def test_client_secret_post_is_used_when_basic_is_not_offered(idp: FakeIdP) -> None:
    idp.discovery_overrides = {"token_endpoint_auth_methods_supported": ["client_secret_post"]}
    assert discover(idp.issuer, allow_http=True).token_auth_method == "client_secret_post"


@pytest.mark.django_db
def test_unadvertised_pkce_is_allowed(idp: FakeIdP) -> None:
    idp.discovery_overrides = {"code_challenge_methods_supported": None}
    discover(idp.issuer, allow_http=True)


@pytest.mark.parametrize(
    ("issuer", "message"),
    [
        ("http://login.example.com", "must be an https URL"),
        ("https://user:pw@login.example.com", "must not contain credentials"),
        ("https://login.example.com/?x=1", "must not have a query"),
        ("javascript:alert(1)", "must be an https URL"),
    ],
)
def test_issuer_urls_are_checked_before_any_request(issuer: str, message: str) -> None:
    with pytest.raises(DiscoveryError, match=message):
        discover(issuer)


def test_https_endpoints_are_required_in_production(idp: FakeIdP) -> None:
    with pytest.raises(DiscoveryError, match="https"):
        discover(idp.issuer, allow_http=False)


def test_unreachable_provider() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(DiscoveryError, match="Couldn't connect"):
        discover(f"http://127.0.0.1:{port}", allow_http=True)


@pytest.fixture
def odd_server() -> Iterator[tuple[int, dict[str, object]]]:
    behaviour: dict[str, object] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if behaviour.get("redirect"):
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
                self.end_headers()
                return
            body = behaviour.get("body", b"{}")
            assert isinstance(body, bytes)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], behaviour
    server.shutdown()
    server.server_close()
    thread.join()


def test_redirects_are_not_followed(odd_server: tuple[int, dict[str, object]]) -> None:
    port, behaviour = odd_server
    behaviour["redirect"] = True
    with pytest.raises(DiscoveryError, match="redirects elsewhere"):
        discover(f"http://127.0.0.1:{port}", allow_http=True)


def test_oversized_documents_are_refused(odd_server: tuple[int, dict[str, object]]) -> None:
    port, behaviour = odd_server
    behaviour["body"] = b"[" + b"0," * 300_000 + b"0]"
    with pytest.raises(DiscoveryError, match="too large"):
        discover(f"http://127.0.0.1:{port}", allow_http=True)


def test_non_json_documents_are_refused(odd_server: tuple[int, dict[str, object]]) -> None:
    port, behaviour = odd_server
    behaviour["body"] = b"<html>login</html>"
    with pytest.raises(DiscoveryError, match="didn't return JSON"):
        discover(f"http://127.0.0.1:{port}", allow_http=True)
