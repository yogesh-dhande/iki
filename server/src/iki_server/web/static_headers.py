"""Headers for some static files, which ServeStatic adds as it serves them.

Kept apart from the rest of the web code so settings can name it without importing Django.
"""

from collections.abc import MutableMapping

#: Iki's viewers' code, which their frames load (iki_server.web.viewers).
VIEWERS_PREFIX = "viewers/"
#: The web app's Web Workers (web/app/src/workers), which Vite names so.
WORKERS_PREFIX = "app/assets/worker-"
#: A worker's policy comes from its script's response, not its page's (CSP 3). The parsers'
#: worker loads its WebAssembly module from Iki's origin and runs it, and nothing else:
#: no eval, no other origin.
WORKER_CSP = (
    "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; connect-src 'self'; "
    "report-uri /csp-report/"
)


def add_headers(headers: MutableMapping[str, str], path: str, url: str) -> None:
    """Viewers' frames are sandboxed, so their documents' origins are no one's: to them,
    the code they load is another origin's. Iki's code is public, and ServeStatic already
    allows every origin to read it (CORS); this lets them use it too.

    Workers' scripts get a policy of their own, which is the one they run under."""
    if f"/{VIEWERS_PREFIX}" in url:
        headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    if f"/{WORKERS_PREFIX}" in url and url.endswith(".js"):
        headers["Content-Security-Policy"] = WORKER_CSP
