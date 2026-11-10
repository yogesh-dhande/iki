"""A small SAML 2.0 identity provider for tests.

It serves real metadata over HTTP and answers Iki's AuthnRequests with real
responses, signed with its own key (assertion-level signatures, SHA-256),
the way Entra ID and Okta do. Tests play the browser: they read the redirect
to the provider, call :meth:`FakeSamlIdP.respond` to "sign in", and post the
result to Iki's ACS URL. Knobs let tests break the response in the ways an
attacker or a misconfigured provider would.
"""

import base64
import http.server
import secrets
import threading
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from onelogin.saml2.constants import OneLogin_Saml2_Constants
from onelogin.saml2.utils import OneLogin_Saml2_Utils

SAMLP = "urn:oasis:names:tc:SAML:2.0:protocol"
SAML = "urn:oasis:names:tc:SAML:2.0:assertion"
EMAIL_FORMAT = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
PERSISTENT_FORMAT = "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent"


def make_key_and_certificate(
    days: int = 365, bits: int = 2048
) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fake SAML IdP")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1) if days > 0 else now + timedelta(days=days - 2))
        .not_valid_after(now + timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _pem_key(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _cert_b64(cert: x509.Certificate) -> str:
    return base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()


def _instant(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class AuthnRequest:
    id: str
    issuer: str
    acs_url: str
    relay_state: str


@dataclass
class FakeSamlIdP:
    port: int
    entity_path: str = "/saml/idp"
    key: rsa.RSAPrivateKey = field(init=False)
    cert: x509.Certificate = field(init=False)
    #: Replaces the metadata the server returns (to test validation).
    metadata_override: str | None = None

    def __post_init__(self) -> None:
        self.key, self.cert = make_key_and_certificate()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def entity_id(self) -> str:
        return self.base + self.entity_path

    @property
    def sso_url(self) -> str:
        return f"{self.entity_id}/sso"

    @property
    def metadata_url(self) -> str:
        return f"{self.entity_id}/metadata"

    def metadata(self, cert: x509.Certificate | None = None) -> str:
        return f"""<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
    xmlns:ds="http://www.w3.org/2000/09/xmldsig#" entityID="{self.entity_id}">
  <md:IDPSSODescriptor WantAuthnRequestsSigned="false"
      protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:KeyDescriptor use="signing">
      <ds:KeyInfo><ds:X509Data><ds:X509Certificate>{_cert_b64(cert or self.cert)}</ds:X509Certificate></ds:X509Data></ds:KeyInfo>
    </md:KeyDescriptor>
    <md:NameIDFormat>{EMAIL_FORMAT}</md:NameIDFormat>
    <md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
        Location="{self.sso_url}"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>"""

    @staticmethod
    def read_request(redirect_url: str) -> AuthnRequest:
        query = {k: v[0] for k, v in parse_qs(urlsplit(redirect_url).query).items()}
        xml = zlib.decompress(base64.b64decode(query["SAMLRequest"]), -15)
        root = ElementTree.fromstring(xml)  # noqa: S314 - Iki's own request, in a test
        issuer = root.find(f"{{{SAML}}}Issuer")
        assert issuer is not None and issuer.text
        return AuthnRequest(
            id=root.get("ID", ""),
            issuer=issuer.text,
            acs_url=root.get("AssertionConsumerServiceURL", ""),
            relay_state=query.get("RelayState", ""),
        )

    def respond(
        self,
        request: AuthnRequest | None,
        *,
        name_id: str,
        attributes: dict[str, str] | None = None,
        name_id_format: str = PERSISTENT_FORMAT,
        sign_assertion: bool = True,
        sign_response: bool = False,
        key: rsa.RSAPrivateKey | None = None,
        audience: str | None = None,
        acs_url: str = "https://testserver/accounts/saml/sso/acs/",
        expires_in: timedelta = timedelta(minutes=5),
        digest: str = OneLogin_Saml2_Constants.SHA256,
        tamper: bool = False,
    ) -> str:
        """A base64 SAMLResponse. ``request=None`` makes an IdP-initiated response."""
        now = datetime.now(UTC)
        in_response_to = f' InResponseTo="{request.id}"' if request else ""
        acs = request.acs_url if request else acs_url
        sp_entity = audience or (request.issuer if request else acs.replace("/acs/", "/metadata/"))
        assertion_id = "_a" + secrets.token_hex(16)
        statements = "".join(
            f"<saml:Attribute Name={quoteattr(name)}><saml:AttributeValue>{escape(value)}"
            "</saml:AttributeValue></saml:Attribute>"
            for name, value in (attributes or {}).items()
        )
        assertion = f"""<saml:Assertion xmlns:saml="{SAML}" ID="{assertion_id}" Version="2.0" IssueInstant="{_instant(now)}">
<saml:Issuer>{escape(self.entity_id)}</saml:Issuer>
<saml:Subject>
<saml:NameID Format="{name_id_format}">{escape(name_id)}</saml:NameID>
<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
<saml:SubjectConfirmationData NotOnOrAfter="{_instant(now + expires_in)}" Recipient="{acs}"{in_response_to}/>
</saml:SubjectConfirmation>
</saml:Subject>
<saml:Conditions NotBefore="{_instant(now - timedelta(minutes=1))}" NotOnOrAfter="{_instant(now + expires_in)}">
<saml:AudienceRestriction><saml:Audience>{escape(sp_entity)}</saml:Audience></saml:AudienceRestriction>
</saml:Conditions>
<saml:AuthnStatement AuthnInstant="{_instant(now)}" SessionIndex="{assertion_id}">
<saml:AuthnContext><saml:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport</saml:AuthnContextClassRef></saml:AuthnContext>
</saml:AuthnStatement>
{f"<saml:AttributeStatement>{statements}</saml:AttributeStatement>" if statements else ""}
</saml:Assertion>"""
        signing_key = _pem_key(key or self.key)
        cert_pem = self.cert.public_bytes(serialization.Encoding.PEM).decode()
        if sign_assertion:
            assertion = OneLogin_Saml2_Utils.add_sign(
                assertion,
                signing_key,
                cert_pem,
                sign_algorithm=OneLogin_Saml2_Constants.RSA_SHA256,
                digest_algorithm=digest,
            ).decode()
            assertion = assertion.removeprefix('<?xml version="1.0"?>\n')
        if tamper:
            assertion = assertion.replace(escape(name_id), "mallory@example.com")
        response = f"""<samlp:Response xmlns:samlp="{SAMLP}" xmlns:saml="{SAML}" ID="_r{secrets.token_hex(16)}" Version="2.0" IssueInstant="{_instant(now)}" Destination="{acs}"{in_response_to}>
<saml:Issuer>{escape(self.entity_id)}</saml:Issuer>
<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>
{assertion}
</samlp:Response>"""
        if sign_response:
            response = OneLogin_Saml2_Utils.add_sign(
                response,
                signing_key,
                cert_pem,
                sign_algorithm=OneLogin_Saml2_Constants.RSA_SHA256,
                digest_algorithm=OneLogin_Saml2_Constants.SHA256,
            ).decode()
        return base64.b64encode(response.encode()).decode()


def serve(idp: FakeSamlIdP) -> tuple[http.server.ThreadingHTTPServer, threading.Thread]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if urlsplit(self.path).path != urlsplit(idp.metadata_url).path:
                self.send_response(404)
                self.end_headers()
                return
            body = (idp.metadata_override or idp.metadata()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/samlmetadata+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", idp.port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
