"""SAML 2.0: reading an identity provider's metadata, and how Iki checks responses.

The wizard reads the provider's metadata once, from a URL or pasted XML, and
stores what it needs: the entity ID, the single sign-on URL and the signing
certificate. The certificate is pinned. It changes only when an
administrator runs the setup wizard again, never because a URL started
serving different metadata.

Responses are checked strictly (ADR 0026):

- the assertion must be signed with the pinned certificate, using SHA-256 or
  stronger;
- the response must answer a sign-in that Iki started (no IdP-initiated
  sign-in), so a captured response can't be replayed into another session;
- python3-saml checks the audience, recipient, destination and validity
  window, and parses XML without DTDs or external entities.
"""

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from django.conf import settings
from django.utils import timezone
from onelogin.saml2.constants import OneLogin_Saml2_Constants
from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser

from iki_server.accounts.oidc import DiscoveryError, check_url, fetch

if TYPE_CHECKING:
    from iki_server.accounts.models import IdentityProvider

#: SAML attribute names Iki reads, first match wins. They cover the standard
#: OIDs and the names Microsoft Entra ID, Okta and Keycloak send by default.
ATTRIBUTE_MAPPING: dict[str, list[str]] = {
    "uid": ["urn:oasis:names:tc:SAML:attribute:subject-id"],
    "email": [
        "urn:oid:0.9.2342.19200300.100.1.3",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
        "email",
        "mail",
        "Email",
    ],
    "first_name": [
        "urn:oid:2.5.4.42",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname",
        "firstName",
        "givenName",
        "given_name",
    ],
    "last_name": [
        "urn:oid:2.5.4.4",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname",
        "lastName",
        "surname",
        "sn",
        "family_name",
    ],
    "name": [
        "urn:oid:2.16.840.1.113730.3.1.241",
        "http://schemas.microsoft.com/identity/claims/displayname",
        "displayName",
        "name",
    ],
}

MIN_RSA_BITS = 2048


@dataclass(frozen=True)
class IdpMetadata:
    entity_id: str
    sso_url: str
    certificate: str
    """PEM."""
    certificate_expires_at: datetime


def _pem(der: bytes) -> str:
    body = base64.b64encode(der).decode()
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(lines) + "\n-----END CERTIFICATE-----\n"


def _certificate(b64: str) -> tuple[str, datetime]:
    try:
        der = base64.b64decode("".join(b64.split()), validate=True)
        cert = x509.load_der_x509_certificate(der)
    except (binascii.Error, ValueError):
        raise DiscoveryError("The metadata's signing certificate can't be read.") from None
    expires = cert.not_valid_after_utc
    if expires <= timezone.now():
        raise DiscoveryError(
            f"The provider's signing certificate expired on {expires:%Y-%m-%d}. "
            "Renew it at the identity provider, then try again."
        )
    key = cert.public_key()
    if isinstance(key, rsa.RSAPublicKey):
        if key.key_size < MIN_RSA_BITS:
            raise DiscoveryError(
                f"The signing key is RSA-{key.key_size}; Iki needs at least RSA-{MIN_RSA_BITS}."
            )
    elif not isinstance(key, ec.EllipticCurvePublicKey):
        raise DiscoveryError("The signing key must be RSA or elliptic curve.")
    return _pem(der), expires


def parse_metadata(xml: str | bytes) -> IdpMetadata:
    """Reads an identity provider's SAML metadata. Errors are safe to show."""
    try:
        parsed = OneLogin_Saml2_IdPMetadataParser.parse(
            xml, required_sso_binding=OneLogin_Saml2_Constants.BINDING_HTTP_REDIRECT
        )
    except Exception:
        raise DiscoveryError("That isn't valid SAML metadata XML.") from None
    idp: dict[str, Any] = parsed.get("idp") or {}
    entity_id = idp.get("entityId")
    sso_url = (idp.get("singleSignOnService") or {}).get("url")
    if not entity_id:
        raise DiscoveryError("The metadata doesn't describe a SAML identity provider.")
    if not sso_url:
        raise DiscoveryError(
            "The metadata has no single sign-on URL with the HTTP-Redirect binding."
        )
    signing = idp.get("x509cert") or next(
        iter((idp.get("x509certMulti") or {}).get("signing", [])), None
    )
    if not signing:
        raise DiscoveryError("The metadata has no signing certificate.")
    certificate, expires = _certificate(signing)
    check_url(sso_url, allow_http=not settings.HTTPS, what="single sign-on URL", allow_query=True)
    if len(entity_id) > 500:
        raise DiscoveryError("The provider's entity ID is too long.")
    return IdpMetadata(
        entity_id=entity_id,
        sso_url=sso_url,
        certificate=certificate,
        certificate_expires_at=expires,
    )


def fetch_metadata(url: str) -> IdpMetadata:
    check_url(url, allow_http=not settings.HTTPS, what="metadata URL", allow_query=True)
    body = fetch(
        url,
        accept="application/samlmetadata+xml, application/xml, text/xml",
        what="metadata document",
        redirect_hint="Enter the metadata URL the identity provider shows for this app.",
    )
    return parse_metadata(body)


def app_settings(provider: "IdentityProvider") -> dict[str, Any]:
    """allauth's settings for a SAML provider: Iki's strict defaults."""
    return {
        "attribute_mapping": ATTRIBUTE_MAPPING,
        "idp": {
            "entity_id": provider.issuer,
            "sso_url": provider.authorization_endpoint,
            "x509cert": provider.saml_certificate,
        },
        "advanced": {
            "strict": True,
            "want_assertion_signed": True,
            "want_message_signed": False,
            "want_attribute_statement": False,
            "reject_deprecated_algorithm": True,
            "reject_idp_initiated_sso": True,
            "authn_request_signed": False,
            "digest_algorithm": OneLogin_Saml2_Constants.SHA256,
            "signature_algorithm": OneLogin_Saml2_Constants.RSA_SHA256,
            # Only development installations use single-label hosts (localhost).
            "allow_single_label_domains": not settings.HTTPS,
        },
    }
