"""The frames Iki's own viewers run in: Mol*, for the structure viewer (ADR 0085).

Mol* parses whatever file it's given, with a great deal of code, so it runs in a frame
the web app opens with ``sandbox="allow-scripts"``: its document's origin is no one's,
so a file that fooled it couldn't read Iki's pages, cookies or storage, or call the API
as the person. The page reads the file and hands its bytes to the frame.

The frame's policy allows its module (by address, with its hash as the script's
integrity) and nothing else: no network, no other scripts, no forms, no frames.
Its module is built by Vite (``web/app/vite.structure.config.ts``) into the static
files, named by its hash, and served to any origin, since to a sandboxed frame every
origin is another one (``static_headers``).
"""

import base64
import hashlib
import json
from functools import cache
from pathlib import Path
from typing import Any

from django.conf import settings
from django.contrib.staticfiles import finders
from django.http import HttpRequest, HttpResponse
from django.templatetags.static import static
from django.utils.cache import patch_cache_control
from django.utils.csp import CSP
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET

from iki_server.components.views import public_origin
from iki_server.web import frames

MANIFEST = "viewers/manifest.json"
STRUCTURE_ENTRY = "src/structure-frame/main.ts"


class ViewersNotBuilt(Exception):  # noqa: N818 - reads as "the viewers are not built"
    pass


def _found(name: str) -> Path | None:
    found = finders.find(name)
    if isinstance(found, str):
        return Path(found)
    collected = Path(settings.STATIC_ROOT) / name
    return collected if collected.is_file() else None


@cache
def _read(path: Path, mtime: float) -> bytes:
    return path.read_bytes()


def _file(name: str) -> bytes:
    path = _found(name)
    if path is None:
        raise ViewersNotBuilt(f"{name} is missing. Build the web app: make web")
    return _read(path, path.stat().st_mtime)


def _module(entry: str) -> tuple[str, str]:
    """The entry's module, as a static file's name, and its integrity."""
    manifest: dict[str, Any] = json.loads(_file(MANIFEST))
    name = "viewers/" + manifest[entry]["file"]
    digest = hashlib.sha384(_file(name)).digest()
    return name, "sha384-" + base64.b64encode(digest).decode()


def _page(module: str, integrity: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="color-scheme" content="light dark">\n'
        "<title>Structure viewer</title>\n"
        f"{frames.HARDEN_TAG}"
        "<style>html, body { margin: 0; height: 100%; } "
        "#structure { position: absolute; inset: 0; }</style>\n"
        f'<script type="module" src="{module}" integrity="{integrity}" '
        'crossorigin="anonymous"></script>\n'
        '</head>\n<body><div id="structure"></div></body>\n</html>\n'
    )


@require_GET
@xframe_options_sameorigin
def structure_frame(request: HttpRequest) -> HttpResponse:
    try:
        name, integrity = _module(STRUCTURE_ENTRY)
    except ViewersNotBuilt as error:
        return HttpResponse(str(error), status=503, content_type="text/plain; charset=utf-8")
    module = public_origin(request) + static(name)
    response = HttpResponse(_page(module, integrity), content_type="text/html; charset=utf-8")
    response._csp_config = {  # type: ignore[attr-defined]
        "default-src": [CSP.NONE],
        "script-src": [frames.HARDEN_SOURCE, module],
        # Mol*'s own styles, which its module adds: there's nothing of anyone else's here.
        "style-src": [CSP.UNSAFE_INLINE],
        "img-src": ["data:", "blob:"],
        "font-src": ["data:"],
        # Everything it shows comes from the page: Mol* fetches nothing.
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
    # Its document is no one's, so some browsers (Firefox) check its resource policy as
    # if another origin had asked. Who may frame it is frame-ancestors' to say: only Iki.
    response["Cross-Origin-Resource-Policy"] = "cross-origin"
    # A new build is a new module: browsers check back each time, and keep it otherwise.
    patch_cache_control(response, no_cache=True)
    return response
