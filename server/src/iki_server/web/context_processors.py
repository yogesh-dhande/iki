from typing import Any

import structlog
from django.http import HttpRequest

from iki_server.setup.models import Installation

logger = structlog.get_logger("iki.web")


def installation(request: HttpRequest) -> dict[str, object]:
    """``organization_name`` for page titles and headers. Read-only: no row is created."""
    name = Installation.objects.filter(pk=1).values_list("organization_name", flat=True).first()
    return {"organization_name": name or ""}


def search_palette(request: HttpRequest) -> dict[str, Any]:
    """``search_assets``: the palette's scripts (ADR 0176), so ⌘K opens it on server-rendered
    pages too, for anyone signed in. None when the app isn't built: the page works without
    it, as it does without scripts."""
    from iki_server.web import frontend

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"search_assets": None}
    try:
        return {"search_assets": frontend.page_assets(request, frontend.SEARCH_ENTRY)}
    except frontend.AppNotBuilt:
        logger.warning("search_palette_not_built")
        return {"search_assets": None}
