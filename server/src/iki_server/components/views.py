"""A component's frame and module, as the web app's sandboxed frames load them (ADR 0084).

The web app shows a component in ``<iframe sandbox="allow-scripts">`` pointed at its
frame: a page with Iki's bridge and the component's module, and nothing else. Both are
addressed by the module's digest, so they never change: browsers keep them for ever,
and a new version is a new address. Neither needs the person's session, and neither
gets it: the frame's document has an origin of its own that is no one's, so its module
is a cross-origin request without cookies, and Iki's code is as public as its own
scripts are. What the frame shows comes from the page, through the bridge.

The frame's policy allows the bridge (by its hash) and the module (by its address,
with its digest as the script's integrity) and nothing else: no network, no other
scripts, no forms, no frames. It also says ``sandbox``, so it's sandboxed however
it's opened, and only Iki's pages may frame it.
"""

import base64
import hashlib
import json
from functools import cache
from html import escape
from pathlib import Path

import structlog
from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse
from django.urls import reverse
from django.utils.cache import patch_cache_control
from django.utils.csp import CSP
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET

from iki_server.components import installed
from iki_server.web import frames
from iki_spec.model import Component

logger = structlog.get_logger("iki.components")

#: Iki's bridge in every frame: it runs the component's element (``bridge.js``).
BRIDGE = (Path(__file__).parent / "bridge.js").read_text()
#: Frames and modules are addressed by digest: they never change.
FOR_EVER = 365 * 24 * 60 * 60


def _hash_source(data: bytes) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode()


def _installed(name: str, digest: str) -> Component:
    """The installed component with that name whose module has that digest."""
    component = installed.components().get(name)
    if component is None or component.sha256 != digest:
        raise Http404
    return component


def _module(component: Component) -> bytes:
    try:
        return installed.module(component.qualified_name)
    except installed.ModuleChanged:
        # The image isn't what was built: someone changed the code after it was checked.
        logger.error("component_module_changed", component=component.qualified_name)
        raise Http404 from None


def public_origin(request: HttpRequest) -> str:
    """Iki's origin, as the frame's policy names it: a sandboxed document's own origin
    is no one's, so ``'self'`` can't be relied on to mean Iki."""
    if settings.IKI_PUBLIC_URL:
        return settings.IKI_PUBLIC_URL.rstrip("/")
    return f"{request.scheme}://{request.get_host()}"


def _forever(response: HttpResponse) -> HttpResponse:
    patch_cache_control(response, public=True, max_age=FOR_EVER, immutable=True)
    return response


@cache
def _page(label: str, element: str, module: str, integrity: str) -> str:
    config = json.dumps({"element": element}).replace("<", "\\u003c")
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n<link rel="icon" href="data:,">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f"<title>{escape(label)}</title>\n"
        f"{frames.HARDEN_TAG}"
        "<style>html { font-family: system-ui, sans-serif; } body { margin: 0; }</style>\n"
        f'<script type="application/json" id="iki-component">{config}</script>\n'
        f'<script type="module" src="{escape(module)}" integrity="{integrity}" '
        'crossorigin="anonymous"></script>\n'
        f'<script type="module">{BRIDGE}</script>\n'
        "</head>\n<body></body>\n</html>\n"
    )


@require_GET
@xframe_options_sameorigin
def frame(request: HttpRequest, name: str, digest: str) -> HttpResponse:
    component = _installed(name, digest)
    data = _module(component)
    module = public_origin(request) + reverse("component_module", args=[name, digest])
    page = _page(component.label, component.element, module, _hash_source(data))
    response = HttpResponse(page, content_type="text/html; charset=utf-8")
    response._csp_config = {  # type: ignore[attr-defined]
        "default-src": [CSP.NONE],
        # The bridge and the module; WebAssembly may be compiled, nothing else evaluated.
        "script-src": [
            frames.HARDEN_SOURCE,
            f"'{_hash_source(BRIDGE.encode())}'",
            module,
            CSP.WASM_UNSAFE_EVAL,
        ],
        # Its own code builds its styles: there's nothing of anyone else's to restyle.
        "style-src": [CSP.UNSAFE_INLINE],
        "img-src": ["data:", "blob:"],
        "font-src": ["data:"],
        "media-src": ["data:", "blob:"],
        "worker-src": ["blob:"],
        "connect-src": [CSP.NONE],
        "form-action": [CSP.NONE],
        "frame-ancestors": [CSP.SELF],
        "base-uri": [CSP.NONE],
        "object-src": [CSP.NONE],
        "sandbox": ["allow-scripts"],
        "upgrade-insecure-requests": settings.SECURE_CSP.get("upgrade-insecure-requests"),
        "report-uri": ["/csp-report/"],
        "report-to": ["csp"],
    }
    # A sandboxed frame is no one's, so some browsers (Firefox) check its document's
    # resource policy as if another origin had asked for it. Who may frame it is
    # frame-ancestors' to say, and it says only Iki.
    response["Cross-Origin-Resource-Policy"] = "cross-origin"
    return _forever(response)


@require_GET
def module(request: HttpRequest, name: str, digest: str) -> HttpResponse:
    component = _installed(name, digest)
    response = HttpResponse(_module(component), content_type="text/javascript; charset=utf-8")
    # Frames' origins are no one's, so to them every module is another origin's.
    response["Access-Control-Allow-Origin"] = "*"
    response["Cross-Origin-Resource-Policy"] = "cross-origin"
    return _forever(response)
