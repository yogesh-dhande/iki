"""Home page and CSP violation reports."""

import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import structlog
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from iki_server.org import views as org_views
from iki_server.setup.models import Installation
from iki_server.web.ratelimit import RateLimiter, client_key

logger = structlog.get_logger("iki.csp")

MAX_REPORT_BYTES = 16 * 1024
csp_report_limiter = RateLimiter(limit_per_minute=30)


@require_GET
def home(request: HttpRequest) -> HttpResponse:
    if not Installation.get().is_set_up:
        return redirect("setup:start")
    return org_views.home(request)


def _without_query(url: Any) -> str | None:  # noqa: ANN401
    """Drops the query string and fragment, which can carry secrets."""
    if not isinstance(url, str):
        return None
    if "://" not in url:
        return url[:100]  # keywords such as "inline" or "eval"
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))[:500]


def _reports(payload: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    # report-to: a list of {"type": "csp-violation", "body": {...}}.
    if isinstance(payload, list):
        return [
            r["body"]
            for r in payload
            if isinstance(r, dict)
            and r.get("type") == "csp-violation"
            and isinstance(r.get("body"), dict)
        ]
    # report-uri (older browsers): {"csp-report": {...}} with hyphenated keys.
    if isinstance(payload, dict) and isinstance(payload.get("csp-report"), dict):
        old = payload["csp-report"]
        return [
            {
                "effectiveDirective": old.get("effective-directive")
                or old.get("violated-directive"),
                "blockedURL": old.get("blocked-uri"),
                "documentURL": old.get("document-uri"),
                "disposition": old.get("disposition"),
            }
        ]
    return []


@csrf_exempt  # browsers send reports without cookies or tokens
@require_POST
def csp_report(request: HttpRequest) -> HttpResponse:
    if not csp_report_limiter.allow(client_key(request)):
        return HttpResponse(status=429)
    body = request.read(MAX_REPORT_BYTES + 1)
    if len(body) > MAX_REPORT_BYTES:
        return HttpResponse(status=413)
    try:
        payload = json.loads(body)
    except ValueError:
        return HttpResponse(status=400)
    for report in _reports(payload)[:10]:
        logger.warning(
            "csp_violation",
            directive=str(report.get("effectiveDirective") or "")[:100],
            blocked=_without_query(report.get("blockedURL")),
            document=_without_query(report.get("documentURL")),
            disposition=str(report.get("disposition") or "")[:20],
        )
    return HttpResponse(status=204)
