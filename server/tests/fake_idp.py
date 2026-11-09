"""A small, strict OpenID Connect provider for tests.

It runs a real HTTP server, so Iki's discovery and allauth's token exchange
make real requests. It checks what a real provider would: the client's
credentials, the redirect URI, and the PKCE verifier against the challenge
(S256 only). Tests play the browser: they read the authorization redirect,
call :meth:`FakeIdP.authorize` to "sign in", and follow the callback.
"""

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm


@dataclass
class PendingCode:
    claims: dict[str, Any]
    challenge: str
    redirect_uri: str
    client_id: str


@dataclass
class FakeIdP:
    port: int
    client_id: str = "iki-test"
    client_secret: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    issuer_path: str = "/realms/test"
    #: Overrides merged into the discovery document (to test validation).
    discovery_overrides: dict[str, Any] = field(default_factory=dict)
    codes: dict[str, PendingCode] = field(default_factory=dict)
    tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    token_requests: list[dict[str, Any]] = field(default_factory=list)
    key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def issuer(self) -> str:
        return self.base + self.issuer_path

    def discovery(self) -> dict[str, Any]:
        doc = {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "userinfo_endpoint": f"{self.issuer}/userinfo",
            "jwks_uri": f"{self.issuer}/jwks",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
            "id_token_signing_alg_values_supported": ["RS256"],
        }
        doc.update(self.discovery_overrides)
        return doc

    def authorize(self, authorization_url: str, claims: dict[str, Any]) -> str:
        """Plays the provider's sign-in page; returns the callback URL with a code."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorization_url).query).items()}
        assert query["response_type"] == "code"
        assert query["client_id"] == self.client_id
        assert query["code_challenge_method"] == "S256", "Iki must use PKCE with S256"
        assert {"openid", "email", "profile"} <= set(query["scope"].split())
        code = secrets.token_urlsafe(16)
        self.codes[code] = PendingCode(
            claims={"sub": "subject-1", **claims},
            challenge=query["code_challenge"],
            redirect_uri=query["redirect_uri"],
            client_id=query["client_id"],
        )
        return f"{query['redirect_uri']}?{urlencode({'code': code, 'state': query['state']})}"

    # --- token endpoint ---------------------------------------------------------

    def exchange(
        self, form: dict[str, str], authorization: str | None
    ) -> tuple[int, dict[str, Any]]:
        self.token_requests.append(form)
        if not authorization or not authorization.startswith("Basic "):
            return 401, {"error": "invalid_client"}
        client_id, _, secret = base64.b64decode(authorization[6:]).decode().partition(":")
        if (client_id, secret) != (self.client_id, self.client_secret):
            return 401, {"error": "invalid_client"}
        pending = self.codes.pop(form.get("code", ""), None)
        if pending is None or form.get("grant_type") != "authorization_code":
            return 400, {"error": "invalid_grant"}
        if form.get("redirect_uri") != pending.redirect_uri:
            return 400, {"error": "invalid_grant", "error_description": "redirect_uri"}
        verifier = form.get("code_verifier", "")
        digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        if digest.rstrip(b"=").decode() != pending.challenge:
            return 400, {"error": "invalid_grant", "error_description": "PKCE verification failed"}
        now = int(time.time())
        id_token = jwt.encode(
            {
                "iss": self.issuer,
                "aud": self.client_id,
                "iat": now,
                "exp": now + 300,
                **pending.claims,
            },
            self.key,
            algorithm="RS256",
            headers={"kid": "test"},
        )
        access_token = secrets.token_urlsafe(16)
        self.tokens[access_token] = pending.claims
        return 200, {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 300,
            "id_token": id_token,
        }

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [{**jwk, "kid": "test", "use": "sig", "alg": "RS256"}]}


def serve(idp: FakeIdP) -> tuple[http.server.ThreadingHTTPServer, threading.Thread]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == f"{idp.issuer_path}/.well-known/openid-configuration":
                self._send(200, idp.discovery())
            elif path == f"{idp.issuer_path}/jwks":
                self._send(200, idp.jwks())
            elif path == f"{idp.issuer_path}/userinfo":
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                claims = idp.tokens.get(token)
                self._send(200, claims) if claims else self._send(401, {"error": "invalid_token"})
            else:
                self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:
            if urlsplit(self.path).path != f"{idp.issuer_path}/token":
                self._send(404, {"error": "not_found"})
                return
            length = int(self.headers.get("Content-Length", 0))
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
            status, body = idp.exchange(form, self.headers.get("Authorization"))
            self._send(status, body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", idp.port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
