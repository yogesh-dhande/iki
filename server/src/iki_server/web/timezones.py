"""Which time zone a server-rendered page shows times in: the viewer's, as the web app does.

The web app formats times in the browser, in the browser's zone. Pages the server
renders can't ask the browser, so the app and ``iki.js`` both remember its zone in
the ``iki_time_zone`` cookie (an IANA name, such as ``America/Los_Angeles``). A page
that names a zone in its address (``?tz=``) uses that one instead, so a filtered link
finds the same events for whoever opens it. Without either, or with a name that
isn't a zone, it's UTC. Pages name the zone they used, so no time is ambiguous.
"""

import re
from datetime import UTC, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.http import HttpRequest

COOKIE = "iki_time_zone"
PARAMETER = "tz"
#: An IANA zone name's shape. Anything else (a path, say) is never looked up.
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){0,2}")
_UTC = frozenset({"UTC", "Etc/UTC", "Etc/Universal", "Etc/Zulu", "Universal", "Zulu"})


def zone_named(name: str | None) -> tzinfo | None:
    if not name or len(name) > 64 or not _NAME.fullmatch(name):
        return None
    if name in _UTC:
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def viewer_zone(request: HttpRequest) -> tzinfo:
    """The address's zone, else the viewer's browser's, else UTC."""
    return zone_named(request.GET.get(PARAMETER)) or zone_named(request.COOKIES.get(COOKIE)) or UTC


def zone_name(zone: tzinfo) -> str:
    return "UTC" if zone is UTC else str(zone)
