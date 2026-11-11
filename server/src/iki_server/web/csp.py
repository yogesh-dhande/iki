"""Content Security Policy, built on Django's ``ContentSecurityPolicyMiddleware``.

The policy (``SECURE_CSP`` in settings) allows nothing by default and only
same-origin scripts, styles, images and connections, with no inline code.

Two directives depend on configuration:

- ``form-action``. Sign-in forms post to Iki, which answers with a redirect
  to the identity provider, and browsers apply ``form-action`` to that
  redirect too. So HTML responses also allow the enabled provider's
  authorization origin, plus the one being set up when the session holds a
  setup grant, and on the OAuth consent page the client's redirect origin.
- ``connect-src``. Browsers upload files' parts straight to object storage,
  so pages also allow the one origin part URLs are signed for.
"""

import time
from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.middleware.csp import ContentSecurityPolicyMiddleware as DjangoCSPMiddleware
from django.utils.csp import CSP

_CACHE_SECONDS = 30
_cache: dict[str, Any] = {"at": 0.0, "origins": []}


def _enabled_provider_origins() -> list[str]:
    from iki_server.accounts.models import IdentityProvider

    now = time.monotonic()
    if now - _cache["at"] > _CACHE_SECONDS:
        _cache["origins"] = [
            p.authorization_origin for p in IdentityProvider.objects.filter(is_enabled=True)
        ]
        _cache["at"] = now
    return list(_cache["origins"])


def clear_cache() -> None:
    _cache["at"] = 0.0


def form_action_origins(request: HttpRequest) -> list[str]:
    from iki_server.setup import grants

    origins = _enabled_provider_origins()
    # The OAuth consent page posts, then redirects to the client.
    origins += getattr(request, "iki_form_action_extra", [])
    if getattr(request, "session", None) is not None:
        grant = grants.current(request)
        pending = grant.identity_provider() if grant else None
        if pending is not None:
            origins.append(pending.authorization_origin)
    return sorted(set(origins))


class ContentSecurityPolicyMiddleware(DjangoCSPMiddleware):
    def process_response(self, request: HttpRequest, response: HttpResponse) -> HttpResponse:
        is_html = response.get("Content-Type", "").startswith("text/html")
        if is_html and not hasattr(response, "_csp_config"):
            from iki_server.files import storage

            origins = form_action_origins(request)
            uploads = storage.upload_origin()
            if origins or uploads:
                config = dict(settings.SECURE_CSP)
                if origins:
                    config["form-action"] = [CSP.SELF, *origins]
                if uploads:
                    config["connect-src"] = [CSP.SELF, uploads]
                response._csp_config = config  # type: ignore[attr-defined]
        return super().process_response(request, response)
