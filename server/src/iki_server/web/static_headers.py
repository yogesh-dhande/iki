"""Headers for some static files, which ServeStatic adds as it serves them.

Kept apart from the rest of the web code so settings can name it without importing Django.
"""

from collections.abc import MutableMapping

#: Iki's viewers' code, which their frames load (iki_server.web.viewers).
VIEWERS_PREFIX = "viewers/"


def add_headers(headers: MutableMapping[str, str], path: str, url: str) -> None:
    """Viewers' frames are sandboxed, so their documents' origins are no one's: to them,
    the code they load is another origin's. Iki's code is public, and ServeStatic already
    allows every origin to read it (CORS); this lets them use it too."""
    if f"/{VIEWERS_PREFIX}" in url:
        headers["Cross-Origin-Resource-Policy"] = "cross-origin"
