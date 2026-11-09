"""Error views that need more than a template."""

import structlog
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

logger = structlog.get_logger("iki.security")


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """Replaces Django's CSRF failure page with one that explains what to do."""
    logger.warning("csrf_failure", reason=reason)
    return render(request, "web/csrf_failure.html", status=403)
