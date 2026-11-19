"""Which provisioned account a person's first sign-in is (ADR 0151).

An account is keyed by the provider's subject identifier (ADR 0023). The one
time Iki links a sign-in to an account by anything else is a person's first
sign-in to the account SCIM provisioned for them, or to the placeholder a
bundle's records named (ADR 0096). It links only by what the identity
provider vouches for, strongest first:

1. **The person's immutable ID at the provider**, as SCIM's ``externalId``:
   OpenID Connect's ``sub`` (Okta's user ID); Microsoft Entra ID's object ID
   (``oid``, in the tenant the provider's issuer names, since Entra's ``sub``
   differs per app); or the SAML subject, or Entra's ``objectidentifier``
   attribute.
2. **The SAML subject, as SCIM's ``userName``**: Entra and Okta send the same
   sign-in name as both by default.
3. **An email address the provider verified**: OpenID Connect's
   ``email_verified``, or Entra's ``xms_edov``, must be true; a SAML
   assertion's ``email_verified`` attribute must be true, or its subject must
   be the address itself. It matches the account's email or SCIM ``userName``.

An address the provider doesn't say it verified links nobody: some providers
let people type in any address. When two accounts answer to the same rule,
the provider's data is ambiguous, and nobody is linked.
"""

import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from django.db.models import Q

from iki_server.accounts.models import IdentityProvider, User

#: The hosts of Microsoft Entra ID's issuers and SAML entity IDs, in each of its clouds.
ENTRA_HOSTS = frozenset(
    {
        "login.microsoftonline.com",
        "login.microsoftonline.us",
        "login.partner.microsoftonline.cn",
        "login.chinacloudapi.cn",
        "sts.windows.net",
        "sts.chinacloudapi.cn",
    }
)
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

MatchedBy = Literal["external_id", "user_name", "email"]


def entra_tenant(issuer: str) -> str | None:
    """The Entra ID tenant an issuer or entity ID names, such as
    ``https://login.microsoftonline.com/<tenant>/v2.0``; None for any other provider."""
    parts = urlsplit(issuer)
    if parts.scheme != "https" or (parts.hostname or "") not in ENTRA_HOSTS:
        return None
    first = parts.path.strip("/").split("/")[0].lower()
    return first if _GUID.match(first) else None


def truthy(value: Any) -> bool:  # noqa: ANN401 - a claim or attribute
    """A claim that is true: JSON's ``true``, or the string some providers send instead."""
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in ("true", "1")


@dataclass(frozen=True)
class Vouched:
    """What the identity provider vouches for about who signed in, beyond the subject."""

    #: The person's immutable IDs at the provider, any of which SCIM may send as externalId.
    ids: frozenset[str] = frozenset()
    #: The SAML subject, which SCIM sends as userName.
    user_name: str = ""
    #: Whether the provider verified the sign-in's email address.
    email_verified: bool = False


def from_oidc(claims: dict[str, Any], provider: IdentityProvider) -> Vouched:
    ids = {str(claims["sub"])} if claims.get("sub") else set()
    tenant = entra_tenant(provider.issuer)
    entra = tenant is not None and str(claims.get("tid", "")).lower() == tenant
    if entra and claims.get("oid"):
        ids.add(str(claims["oid"]).lower())
    verified = truthy(claims.get("email_verified")) or (entra and truthy(claims.get("xms_edov")))
    return Vouched(ids=frozenset(ids), email_verified=verified)


def from_saml(
    subject: str, data: dict[str, Any], email: str, provider: IdentityProvider
) -> Vouched:
    """``data`` is the response's attributes as :data:`~iki_server.accounts.saml.ATTRIBUTE_MAPPING`
    maps them."""
    ids = {subject} if subject else set()
    tenant = entra_tenant(provider.issuer)
    in_tenant = tenant is not None and str(data.get("tenant_id") or "").lower() == tenant
    if in_tenant and data.get("object_id"):
        ids.add(str(data["object_id"]).lower())
    verified = truthy(data.get("email_verified")) or (
        bool(email) and email.lower() == subject.strip().lower()
    )
    return Vouched(ids=frozenset(ids), user_name=subject, email_verified=verified)


@dataclass(frozen=True)
class Link:
    account: User
    matched_by: MatchedBy


def provisioned_account(vouched: Vouched, email: str) -> Link | None:
    """The provisioned account this sign-in is, by the strongest rule that names exactly one."""
    rules: list[tuple[MatchedBy, Q | None]] = [
        ("external_id", Q(scim_external_id__in=vouched.ids) if vouched.ids else None),
        ("user_name", Q(scim_user_name=vouched.user_name) if vouched.user_name else None),
        (
            "email",
            Q(email=email) | Q(scim_user_name=email) if email and vouched.email_verified else None,
        ),
    ]
    provisioned = User.objects.filter(provisioned_at__isnull=False)
    for matched_by, rule in rules:
        if rule is None:
            continue
        found = list(provisioned.filter(rule)[:2])
        if len(found) == 1:
            return Link(found[0], matched_by)
        if found:
            return None  # ambiguous: two accounts answer to what the provider sent
    return None


def provisioned_by_address(email: str) -> bool:
    """Whether a provisioned account has this address, as its email or SCIM userName."""
    return (
        User.objects.filter(provisioned_at__isnull=False)
        .filter(Q(email=email) | Q(scim_user_name=email))
        .exists()
    )
