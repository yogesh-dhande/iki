"""Signing in again to sign, through the organization's identity provider (ADR 0124).

Iki has no passwords (ADR 0023), so the identity provider checks who's signing, with
whatever it asks for: a password and a second factor, a passkey. Each signature asks it
to sign the person in again, however recently they did:

- **OpenID Connect**: an authorization request with ``prompt=login`` and ``max_age=0``,
  its own ``state``, ``nonce`` and PKCE verifier. The ID token's signature, issuer,
  audience and expiry are checked (allauth's ``jwtkit``), then that its ``nonce`` is this
  request's, its ``sub`` is the person's, and its ``auth_time`` is after they started
  signing.
- **SAML**: an AuthnRequest with ``ForceAuthn="true"``. The response must answer it
  (``InResponseTo``), be signed as sign-in's are (ADR 0026), name the person, and its
  ``AuthnInstant`` must be after they started signing.

Both come back to the URLs sign-in uses, so nothing more is registered at the provider:
the callback and the ACS hand a response to :func:`oidc_callback` or :func:`saml_finish`
when its ``state`` or ``RelayState`` is a signing's in this session, and to allauth
otherwise. Nobody is signed in by it, and the session stays as it was.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import requests
import structlog
from allauth.core.internal import httpkit
from allauth.socialaccount.internal import jwtkit
from allauth.socialaccount.providers.oauth2.client import OAuth2Error
from allauth.socialaccount.providers.openid_connect import views as oidc_views
from allauth.socialaccount.providers.openid_connect.views import OpenIDConnectOAuth2Adapter
from allauth.socialaccount.providers.saml import views as saml_views
from allauth.socialaccount.providers.saml.utils import build_saml_config, prepare_django_request
from allauth.socialaccount.sessions import LoginSession
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from onelogin.saml2.auth import OneLogin_Saml2_Auth
from onelogin.saml2.errors import OneLogin_Saml2_Error
from onelogin.saml2.response import OneLogin_Saml2_Response
from onelogin.saml2.utils import OneLogin_Saml2_Utils

from iki_server.accounts.models import IdentityProvider
from iki_server.signatures import services
from iki_server.signatures.models import Signing

logger = structlog.get_logger("iki.signatures")

#: Session key: the signings waiting for the identity provider, by their ``state``.
SESSION_KEY = "_iki_signing"
#: At most this many wait at once in a session; older ones are dropped.
MAX_WAITING = 5
#: The ID token algorithms Iki accepts: never ``none``, nor HMAC with a public secret.
ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)
#: OIDC errors that mean the person stopped, not that anything went wrong.
CANCELLED = frozenset({"access_denied", "login_required", "interaction_required"})


def _waiting(request: HttpRequest) -> dict[str, dict[str, Any]]:
    found = request.session.get(SESSION_KEY)
    return found if isinstance(found, dict) else {}


def _keep(request: HttpRequest, state: str, entry: dict[str, Any]) -> None:
    waiting = _waiting(request)
    waiting[state] = {**entry, "at": time.time()}
    if len(waiting) > MAX_WAITING:
        for old in sorted(waiting, key=lambda k: waiting[k].get("at", 0))[:-MAX_WAITING]:
            waiting.pop(old)
    request.session[SESSION_KEY] = waiting


def _take(request: HttpRequest, state: str) -> dict[str, Any] | None:
    waiting = _waiting(request)
    entry = waiting.pop(state, None)
    if entry is not None:
        request.session[SESSION_KEY] = waiting
    return entry


def back(signing: Signing) -> HttpResponse:
    """To the page the person started signing on, which shows how it went."""
    parts = urlsplit(signing.return_to)
    query = (parts.query + "&" if parts.query else "") + urlencode({"signing": str(signing.pk)})
    return redirect(f"{parts.path}?{query}")


def _gone(request: HttpRequest, status: int = 404) -> HttpResponse:
    return render(request, "signatures/gone.html", status=status)


@never_cache
@require_GET
@login_required
def start(request: HttpRequest, signing_id: str) -> HttpResponse:
    """Sends the person to the identity provider to sign in again, for a signing they
    started in this session."""
    try:
        signing = services.started(signing_id, request.user, request.session.session_key or "")  # type: ignore[arg-type]
    except (ValueError, ValidationError):
        signing = None
    if signing is None:
        return _gone(request)
    provider = signing.provider
    if not provider.is_enabled:
        return back(
            services.fail(
                signing,
                services.Failure("provider", "That identity provider isn't the one in use now."),
            )
        )
    try:
        if provider.is_saml:
            return _saml_start(request, signing, provider)
        return _oidc_start(request, signing, provider)
    except (requests.RequestException, OneLogin_Saml2_Error, KeyError, ValueError) as error:
        logger.warning("signing_start_failed", error=type(error).__name__)
        return back(
            services.fail(
                signing,
                services.Failure(
                    "provider", "Iki couldn't reach the identity provider: try again in a minute."
                ),
            )
        )


# --- OpenID Connect -----------------------------------------------------------------------------


def _adapter(request: HttpRequest, provider: IdentityProvider) -> OpenIDConnectOAuth2Adapter:
    return OpenIDConnectOAuth2Adapter(request, provider.provider_id)


def _oidc_start(request: HttpRequest, signing: Signing, provider: IdentityProvider) -> HttpResponse:
    adapter = _adapter(request, provider)
    app = adapter.get_provider().app
    config = adapter.openid_config
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    params = {
        "client_id": app.client_id,
        "redirect_uri": adapter.get_callback_url(request, app),
        "response_type": "code",
        "scope": "openid profile email",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge.rstrip(b"=").decode(),
        "code_challenge_method": "S256",
        # Sign in again, whatever session the provider has (OIDC Core 3.1.2.1).
        "prompt": "login",
        "max_age": "0",
        "login_hint": signing.user.email,
    }
    if settings.IKI_SIGNING_AUTHN_CONTEXTS:
        params["acr_values"] = " ".join(settings.IKI_SIGNING_AUTHN_CONTEXTS)
    _keep(
        request,
        state,
        {"signing": str(signing.pk), "protocol": "oidc", "nonce": nonce, "verifier": verifier},
    )
    return redirect(f"{config['authorization_endpoint']}?{urlencode(params)}")


def oidc_callback(request: HttpRequest, provider_id: str) -> HttpResponse:
    """The provider's redirect back: a signing's, if its ``state`` is one this session
    waits for, else a sign-in's, for allauth."""
    state = request.GET.get("state", "")
    if state and state in _waiting(request) and request.user.is_authenticated:
        entry = _take(request, state)
        assert entry is not None  # noqa: S101 - just checked
        return _oidc_finish(request, provider_id, entry)
    return oidc_views.callback(request, provider_id)  # type: ignore[no-any-return]


def _oidc_finish(request: HttpRequest, provider_id: str, entry: dict[str, Any]) -> HttpResponse:
    signing = _signing(request, entry)
    if signing is None:
        return _gone(request)
    try:
        evidence = _oidc_evidence(request, signing, provider_id, entry)
    except services.Failure as failure:
        return back(services.fail(signing, failure))
    return back(services.finish(signing, evidence))


def _oidc_evidence(
    request: HttpRequest, signing: Signing, provider_id: str, entry: dict[str, Any]
) -> services.Evidence:
    provider = signing.provider
    if provider.provider_id != provider_id or provider.is_saml:
        raise services.Failure(
            "invalid", "The answer came from another identity provider.", counts=True
        )
    error = request.GET.get("error", "")
    if error:
        if error in CANCELLED:
            raise services.Failure("cancelled", "You didn't sign in again, so it didn't sign.")
        raise services.Failure(
            "provider", f"The identity provider didn't sign you in again ({error[:60]})."
        )
    code = request.GET.get("code", "")
    if not code:
        raise services.Failure(
            "invalid", "The identity provider's answer had no code.", counts=True
        )
    adapter = _adapter(request, provider)
    app = adapter.get_provider().app
    try:
        config = adapter.openid_config
        tokens = adapter.get_client(request, app).get_access_token(
            code, pkce_code_verifier=entry.get("verifier")
        )
    except (OAuth2Error, requests.RequestException):
        raise services.Failure(
            "provider", "Iki couldn't finish signing you in again at the identity provider."
        ) from None
    id_token = tokens.get("id_token") if isinstance(tokens, dict) else None
    if not isinstance(id_token, str):
        raise services.Failure(
            "invalid", "The identity provider sent no ID token, so it didn't sign.", counts=True
        )
    try:
        import jwt

        if jwt.get_unverified_header(id_token).get("alg") not in ALGORITHMS:
            raise services.Failure(
                "invalid", "The ID token isn't signed as Iki accepts.", counts=True
            )
        claims = jwtkit.verify_and_decode(
            credential=id_token,
            keys_url=config["jwks_uri"],
            issuer=config["issuer"],
            audience=app.client_id,
            lookup_kid=jwtkit.lookup_kid_jwk,
            verify_signature=True,
        )
    except (OAuth2Error, jwt.PyJWTError, requests.RequestException, KeyError):
        raise services.Failure(
            "invalid",
            "The identity provider's ID token didn't check out, so it didn't sign.",
            counts=True,
        ) from None
    if not secrets.compare_digest(str(claims.get("nonce", "")), str(entry.get("nonce", ""))):
        raise services.Failure(
            "invalid", "The ID token answers another request, so it didn't sign.", counts=True
        )
    audience = claims.get("aud")
    if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != app.client_id:
        raise services.Failure("invalid", "The ID token is for another app.", counts=True)
    auth_time = claims.get("auth_time")
    if not isinstance(auth_time, (int, float)) or isinstance(auth_time, bool):
        raise services.Failure(
            "no_auth_time",
            "The identity provider didn't say when you signed in (auth_time), so Iki can't "
            "tell you signed in again. An Org Admin should have it sent.",
        )
    context = str(claims.get("acr") or "")
    wanted = settings.IKI_SIGNING_AUTHN_CONTEXTS
    if wanted and context not in wanted:
        raise services.Failure(
            "weak",
            "The identity provider signed you in without the authentication signing needs ("
            + ", ".join(wanted)
            + ").",
        )
    methods = claims.get("amr")
    return services.Evidence(
        protocol="oidc",
        issuer=str(claims.get("iss", "")),
        subject=str(claims.get("sub", "")),
        authenticated_at=dt.datetime.fromtimestamp(auth_time, dt.UTC),
        methods=tuple(str(m) for m in methods) if isinstance(methods, list) else (),
        context=context,
    )


def _signing(request: HttpRequest, entry: dict[str, Any]) -> Signing | None:
    """The waiting signing an answer is for: the person's own, in this session."""
    try:
        return services.started(
            entry.get("signing"),
            request.user,  # type: ignore[arg-type]
            request.session.session_key or "",
        )
    except (ValueError, ValidationError):
        return None


# --- SAML ---------------------------------------------------------------------------------------


class _Response(OneLogin_Saml2_Response):  # type: ignore[misc]
    def get_authn_instant(self) -> int | None:
        """When the person signed in, from the signed assertion's AuthnStatement."""
        nodes = self._query_assertion("/saml:AuthnStatement")
        if not nodes or not nodes[0].get("AuthnInstant"):
            return None
        return int(OneLogin_Saml2_Utils.parse_SAML_to_time(nodes[0].get("AuthnInstant")))


class _Auth(OneLogin_Saml2_Auth):  # type: ignore[misc]
    response_class = _Response
    authn_instant: int | None = None

    def store_valid_response(self, response: _Response) -> None:
        super().store_valid_response(response)
        self.authn_instant = response.get_authn_instant()


def _saml_config(request: HttpRequest, provider: IdentityProvider) -> dict[str, Any]:
    app = provider.to_social_app()
    config: dict[str, Any] = build_saml_config(request, app.settings, app.client_id)
    if settings.IKI_SIGNING_AUTHN_CONTEXTS:
        config["security"]["requestedAuthnContext"] = list(settings.IKI_SIGNING_AUTHN_CONTEXTS)
        config["security"]["requestedAuthnContextComparison"] = "exact"
    return config


def _saml_start(request: HttpRequest, signing: Signing, provider: IdentityProvider) -> HttpResponse:
    auth = _Auth(prepare_django_request(request), _saml_config(request, provider))
    state = secrets.token_urlsafe(32)
    url = auth.login(return_to=state, force_authn=True)
    _keep(
        request,
        state,
        {"signing": str(signing.pk), "protocol": "saml", "request_id": auth.get_last_request_id()},
    )
    return redirect(url)


def saml_finish(request: HttpRequest, organization_slug: str) -> HttpResponse:
    """Where the ACS sends the browser on: a signing's response, if its ``RelayState`` is
    one this session waits for, else a sign-in's, for allauth."""
    if request.user.is_authenticated and _waiting(request):
        acs = LoginSession(request, "saml_acs_session", "saml-acs-session")
        data = acs.store.get("request")
        if data:
            posted = httpkit.deserialize_request(data, HttpRequest())
            state = posted.POST.get("RelayState", "")
            entry = _waiting(request).get(state)
            if entry is not None and entry.get("protocol") == "saml":
                acs.delete()
                _take(request, state)
                return _saml_finish(request, posted, entry)
    return saml_views.finish_acs(request, organization_slug=organization_slug)  # type: ignore[no-any-return]


def _saml_finish(request: HttpRequest, posted: HttpRequest, entry: dict[str, Any]) -> HttpResponse:
    signing = _signing(request, entry)
    if signing is None:
        return _gone(request)
    try:
        evidence = _saml_evidence(posted, signing, entry)
    except services.Failure as failure:
        return back(services.fail(signing, failure))
    return back(services.finish(signing, evidence))


def _saml_evidence(
    posted: HttpRequest, signing: Signing, entry: dict[str, Any]
) -> services.Evidence:
    provider = signing.provider
    if not provider.is_saml:
        raise services.Failure(
            "invalid", "The answer came from another identity provider.", counts=True
        )
    auth = _Auth(prepare_django_request(posted), _saml_config(posted, provider))
    try:
        auth.process_response(request_id=entry.get("request_id"))
    except (OneLogin_Saml2_Error, ValueError):
        raise services.Failure(
            "invalid", "The identity provider's answer didn't check out.", counts=True
        ) from None
    if auth.get_errors() or not auth.is_authenticated():
        status = auth.get_last_error_reason() or ""
        logger.warning("signing_saml_invalid", errors=auth.get_errors(), reason=status[:200])
        raise services.Failure(
            "invalid",
            "The identity provider's answer didn't check out, so it didn't sign.",
            counts=True,
        )
    if auth.authn_instant is None:
        raise services.Failure(
            "no_auth_time",
            "The identity provider didn't say when you signed in (AuthnInstant), so Iki can't "
            "tell you signed in again.",
        )
    contexts = [str(c) for c in auth.get_last_authn_contexts() or ()]
    wanted = settings.IKI_SIGNING_AUTHN_CONTEXTS
    if wanted and not set(contexts) & set(wanted):
        raise services.Failure(
            "weak",
            "The identity provider signed you in without the authentication signing needs ("
            + ", ".join(wanted)
            + ").",
        )
    social = provider.to_social_app().get_provider(posted)
    return services.Evidence(
        protocol="saml",
        issuer=provider.issuer,
        subject=str(social.extract_uid(auth)),
        authenticated_at=dt.datetime.fromtimestamp(auth.authn_instant, dt.UTC),
        context=contexts[0] if contexts else "",
    )
