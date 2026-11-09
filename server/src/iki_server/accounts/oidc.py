"""OpenID Connect discovery, used to check a provider before it is saved."""

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests

from iki_server import __version__

TIMEOUT = (5, 10)  # connect, read (seconds)
MAX_BYTES = 512 * 1024


class DiscoveryError(Exception):
    """The provider can't be used. The message is safe to show to the administrator."""


@dataclass(frozen=True)
class ProviderMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    token_auth_method: str


def discovery_url(issuer: str) -> str:
    return issuer.rstrip("/") + "/.well-known/openid-configuration"


def check_url(url: str, *, allow_http: bool, what: str) -> None:
    parts = urlsplit(url)
    allowed = {"https", "http"} if allow_http else {"https"}
    if parts.scheme not in allowed or not parts.hostname:
        need = "an http(s)" if allow_http else "an https"
        raise DiscoveryError(f"The {what} must be {need} URL.")
    if parts.username or parts.password:
        raise DiscoveryError(f"The {what} must not contain credentials.")
    if parts.query or parts.fragment:
        raise DiscoveryError(f"The {what} must not have a query or fragment.")


def discover(issuer: str, *, allow_http: bool = False) -> ProviderMetadata:
    """Fetches and validates the provider's discovery document (OpenID Connect Discovery 1.0)."""
    check_url(issuer, allow_http=allow_http, what="issuer URL")
    url = discovery_url(issuer)
    host = urlsplit(issuer).hostname
    try:
        with requests.get(
            url,
            timeout=TIMEOUT,
            allow_redirects=False,
            stream=True,
            headers={"Accept": "application/json", "User-Agent": f"iki-server/{__version__}"},
        ) as response:
            if response.is_redirect:
                raise DiscoveryError(
                    f"{url} redirects elsewhere. "
                    "Enter the issuer exactly as the provider reports it."
                )
            if response.status_code != 200:
                raise DiscoveryError(f"{url} returned HTTP {response.status_code}.")
            body = response.raw.read(MAX_BYTES + 1, decode_content=True)
    except requests.exceptions.SSLError:
        raise DiscoveryError(f"Couldn't verify the TLS certificate of {host}.") from None
    except requests.exceptions.Timeout:
        raise DiscoveryError(f"{host} didn't answer in time.") from None
    except requests.exceptions.ConnectionError:
        raise DiscoveryError(
            f"Couldn't connect to {host}. Check the URL, and that Iki's network policy allows it."
        ) from None
    if len(body) > MAX_BYTES:
        raise DiscoveryError("The discovery document is too large.")
    try:
        document: Any = json.loads(body)
    except ValueError:
        raise DiscoveryError(f"{url} didn't return JSON.") from None
    if not isinstance(document, dict):
        raise DiscoveryError(f"{url} didn't return a JSON object.")
    return _validate(issuer, document, allow_http=allow_http)


def _validate(issuer: str, doc: dict[str, Any], *, allow_http: bool) -> ProviderMetadata:
    if doc.get("issuer") != issuer:
        raise DiscoveryError(
            f"The provider reports its issuer as {doc.get('issuer')!r}, not {issuer!r}. "
            "Enter it exactly as reported, including any trailing slash."
        )
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri", "userinfo_endpoint"):
        value = doc.get(key)
        if not isinstance(value, str) or not value:
            raise DiscoveryError(f"The discovery document has no {key}.")
        check_url(value, allow_http=allow_http, what=key.replace("_", " "))
    response_types = doc.get("response_types_supported") or []
    if "code" not in response_types:
        raise DiscoveryError("The provider doesn't support the authorization code flow.")
    pkce_methods = doc.get("code_challenge_methods_supported")
    # Some providers support PKCE without advertising it; only a list without S256 is fatal.
    if isinstance(pkce_methods, list) and "S256" not in pkce_methods:
        raise DiscoveryError("The provider doesn't support PKCE with S256.")
    auth_methods = doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    if "client_secret_basic" in auth_methods:
        method = "client_secret_basic"
    elif "client_secret_post" in auth_methods:
        method = "client_secret_post"
    else:
        raise DiscoveryError("The provider doesn't accept client secrets at its token endpoint.")
    return ProviderMetadata(
        issuer=issuer,
        authorization_endpoint=doc["authorization_endpoint"],
        token_endpoint=doc["token_endpoint"],
        token_auth_method=method,
    )
